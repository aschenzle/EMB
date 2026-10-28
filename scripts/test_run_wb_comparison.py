"""Integration checks for the repeatable white-box experiment runner."""

import json
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("run-wb-comparison.py")


class ControllerWarningTest(unittest.TestCase):
    """Check that controller errors are visible even after a zero EvoMaster exit."""

    def test_dynamodb_serialization_failure_is_reported(self):
        """A DynamoDB request serialization error warns about incomplete coverage."""
        spec = importlib.util.spec_from_file_location("run_wb_comparison", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "controller.log"
            log.write_text("java.io.NotSerializableException: "
                           "software.amazon.awssdk.services.dynamodb.model.QueryRequest\n")
            self.assertIn("coverage may be incomplete", module.controller_warnings(log)[0])


@unittest.skipUnless(all(shutil.which(tool) for tool in ("java", "javac", "jar")),
                     "a JDK is required")
class RunWbComparisonTest(unittest.TestCase):
    """Exercise real controller and EvoMaster subprocess lifecycles."""

    def test_two_cycles_keep_three_modes_separate_with_the_same_seed(self):
        """Each World Cup cycle retains raw results for all three DynamoDB modes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dist = root / "dist"
            dist.mkdir()
            sources = {
                "Controller": """
                    import java.net.ServerSocket;
                    public class Controller {
                        public static void main(String[] args) throws Exception {
                            try (ServerSocket socket = new ServerSocket(Integer.parseInt(args[0]))) {
                                System.out.println("World Cup controller ready");
                                while (true) socket.accept().close();
                            }
                        }
                    }
                """,
                "EvoMaster": """
                    import java.nio.file.*;
                    public class EvoMaster {
                        public static void main(String[] args) throws Exception {
                            String folder = null, statistics = null, seed = null, port = null;
                            for (String arg : args) {
                                if (arg.startsWith("--outputFolder=")) folder = arg.substring(15);
                                if (arg.startsWith("--statisticsFile=")) statistics = arg.substring(17);
                                if (arg.startsWith("--seed=")) seed = arg.substring(7);
                                if (arg.startsWith("--sutControllerPort=")) port = arg.substring(20);
                                System.out.println(arg);
                            }
                            try (java.net.Socket socket = new java.net.Socket("127.0.0.1", Integer.parseInt(port))) {
                                Files.createDirectories(Paths.get(folder));
                                Files.writeString(Paths.get(folder, "WorldCupTest.java"), "seed=" + seed);
                                Files.writeString(Paths.get(statistics), "seed\\n" + seed + "\\n");
                            }
                        }
                    }
                """,
            }
            for name, source in sources.items():
                (root / f"{name}.java").write_text(source, encoding="utf-8")
                subprocess.run(["javac", str(root / f"{name}.java")], check=True,
                               capture_output=True, text=True)
                target = dist / ("world-cup-evomaster-runner.jar" if name == "Controller"
                                 else "evomaster.jar")
                subprocess.run(["jar", "cfe", str(target), name, "-C", str(root), f"{name}.class"],
                               check=True, capture_output=True, text=True)
            (dist / "world-cup-sut.jar").touch()
            (dist / "evomaster-agent.jar").touch()
            missing_java = subprocess.run([
                sys.executable, str(SCRIPT), "--app", "world-cup", "--runs", "1",
                "--dist", str(dist), "--output-dir", str(root / "invalid"),
                "--java", str(root / "missing-java.exe"),
            ], capture_output=True, text=True)
            self.assertIn("Java executable for --java was not found", missing_java.stderr)
            self.assertFalse((root / "invalid").exists())

            library_jar = dist / "evomaster-core.jar"
            subprocess.run(["jar", "cf", str(library_jar), "-C", str(root), "EvoMaster.class"],
                           check=True, capture_output=True, text=True)
            non_executable = subprocess.run([
                sys.executable, str(SCRIPT), "--app", "world-cup", "--runs", "1",
                "--dist", str(dist), "--output-dir", str(root / "invalid"),
                "--evomaster-jar", str(library_jar),
            ], capture_output=True, text=True)
            self.assertIn("EvoMaster jar is not executable", non_executable.stderr)
            self.assertFalse((root / "invalid").exists())

            result = subprocess.run([
                sys.executable, str(SCRIPT), "--app", "world-cup", "--runs", "2",
                "--seed", "2026", "--dist", str(dist), "--output-dir", str(root / "raw"),
                "--startup-timeout", "10",
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            modes = {
                "no-ddb-heuristics": (False, False, False, False),
                "ddb-heuristics-no-insertions": (True, True, True, False),
                "ddb-heuristics-with-insertions": (True, True, True, True),
            }
            option_names = ("instrumentMR_DYNAMODB", "heuristicsForDynamoDb",
                            "extractDynamoDbExecutionInfo", "generateDynamoDbData")
            for number in (1, 2):
                for mode_name, expected_flags in modes.items():
                    run = root / "raw" / "world-cup" / f"cycle-{number:03d}" / mode_name
                    metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
                    self.assertEqual(number, metadata["cycle"])
                    self.assertEqual(mode_name, metadata["mode"])
                    self.assertEqual(2026, metadata["seed"])
                    self.assertEqual(0, metadata["evomaster_exit_code"])
                    self.assertEqual([], metadata["warnings"])
                    self.assertIn("seed=2026", (run / "tests" / "WorldCupTest.java").read_text())
                    self.assertEqual("seed\n2026\n", (run / "statistics.csv").read_text())
                    log = (run / "evomaster.log").read_text()
                    for option, enabled in zip(option_names, expected_flags):
                        self.assertIn(f"--{option}={str(enabled).lower()}", log)
                    self.assertIn("World Cup controller ready", (run / "controller.log").read_text())

            appended = subprocess.run([
                sys.executable, str(SCRIPT), "--app", "world-cup", "--runs", "2",
                "--dist", str(dist), "--output-dir", str(root / "raw"),
                "--startup-timeout", "10",
            ], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, appended.returncode, appended.stdout + appended.stderr)
            generated_seeds = []
            for cycle in (3, 4):
                mode_seeds = set()
                for mode_name in modes:
                    run = root / "raw" / "world-cup" / f"cycle-{cycle:03d}" / mode_name
                    metadata = json.loads((run / "run.json").read_text(encoding="utf-8"))
                    seed = metadata["seed"]
                    self.assertTrue(1 <= seed < 2**63)
                    self.assertIn(f"--seed={seed}", metadata["evomaster_command"])
                    self.assertEqual(f"seed={seed}",
                                     (run / "tests" / "WorldCupTest.java").read_text())
                    mode_seeds.add(seed)
                self.assertEqual(1, len(mode_seeds))
                generated_seeds.append(mode_seeds.pop())
            self.assertNotEqual(generated_seeds[0], generated_seeds[1])


if __name__ == "__main__":
    unittest.main()
