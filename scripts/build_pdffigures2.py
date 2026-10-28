"""Build the pinned PDFFigures2 CLI with Java 21 (no extraction patches)."""

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import urllib.request

REVISION = "3d7ad46753d4a315cccd1c2bcab398380e88c534"
PATCH = """diff --git a/build.sbt b/build.sbt
--- a/build.sbt
+++ b/build.sbt
@@ -1,5 +1,5 @@
 lazy val scala211 = "2.11.12"
-lazy val scala212 = "2.12.16"
+lazy val scala212 = "2.12.20"
 lazy val scala213 = "2.13.8" // Not supported yet (collections changes required in common)
 lazy val supportedScalaVersions = List(scala212, scala211)
[context-blank]
@@ -21,9 +21,6 @@
   scmInfo := Some(ScmInfo(
     url("https://github.com/allenai/pdffigures2"),
     "https://github.com/allenai/pdffigures2.git")),
-  bintrayPackage := s"${organization.value}:${name.value}_${scalaBinaryVersion.value}",
-  bintrayOrganization := Some("allenai"),
-  bintrayRepository := "maven",
   libraryDependencies ++= Seq(
     "io.spray" %% "spray-json" % "1.3.6",
     "com.github.scopt" %% "scopt" % "4.1.0",
diff --git a/project/plugins.sbt b/project/plugins.sbt
--- a/project/plugins.sbt
+++ b/project/plugins.sbt
@@ -1,5 +1,2 @@
 addSbtPlugin("com.eed3si9n" % "sbt-assembly" % "1.2.0")
[context-blank]
-addSbtPlugin("org.scalameta" % "sbt-scalafmt" % "2.4.6")
-
-addSbtPlugin("org.foundweekends" % "sbt-bintray" % "0.6.1")
""".replace("\n[context-blank]\n", "\n \n")


def build(destination: Path, java="java"):
    destination = destination.resolve()
    with tempfile.TemporaryDirectory(prefix="paperbox-pdffigures2-build-") as temporary:
        root = Path(temporary)
        source = root / "source"
        subprocess.run(
            [
                "git",
                "clone",
                "--no-checkout",
                "https://github.com/allenai/pdffigures2.git",
                str(source),
            ],
            check=True,
            timeout=180,
        )
        subprocess.run(
            ["git", "checkout", "--detach", REVISION], cwd=source, check=True
        )
        subprocess.run(
            ["git", "apply", "-"], cwd=source, input=PATCH.encode(), check=True
        )
        repository = root / "repositories"
        repository.write_text(
            "[repositories]\nlocal\nmaven-central: https://repo.maven.apache.org/maven2\n"
        )
        launcher = root / "sbt-launch.jar"
        url = "https://repo.maven.apache.org/maven2/org/scala-sbt/sbt-launch/1.10.7/sbt-launch-1.10.7.jar"
        with urllib.request.urlopen(url, timeout=60) as response:
            launcher.write_bytes(response.read())
        subprocess.run(
            [
                java,
                "-XX:ActiveProcessorCount=2",
                "-Xmx2g",
                "-Dsbt.version=1.10.7",
                "-Dsbt.override.build.repos=true",
                f"-Dsbt.repository.config={repository}",
                "-jar",
                str(launcher),
                "assembly",
            ],
            cwd=source,
            check=True,
            timeout=900,
        )
        jar = source / "pdffigures2.jar"
        if not jar.is_file():
            raise RuntimeError("assembly did not produce pdffigures2.jar")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(jar, destination)
        print(
            f"PDFFigures2 {REVISION} SHA256 {hashlib.sha256(destination.read_bytes()).hexdigest()}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("data/tools/pdffigures2/pdffigures2.jar")
    )
    parser.add_argument("--java", default="java")
    args = parser.parse_args()
    build(args.output, args.java)
