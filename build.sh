#!/usr/bin/env bash
# Rebuild and stage the YTDLP app.
# Usage:
#   ./build.sh           - recompile changed .java files, update jar, stage runtime files
#   ./build.sh run       - same as above, then launch the app
#   ./build.sh clean     - remove target/ and the staged runtime files
#
# Assumptions:
#   - The IntelliJ artifact jar lives at out/artifacts/YTDLP_jar/YTDLP.jar
#     (build it once via IntelliJ: Build > Build Artifacts > YTDLP:jar > Build).
#     After that, this script keeps it in sync with the source.
#   - JavaFX SDK is at $JAVAFX_HOME or the default below.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

JAR_DIR="$PROJECT_ROOT/out/artifacts/YTDLP_jar"
JAR_PATH="$JAR_DIR/YTDLP.jar"
SRC_PKG_DIR="$PROJECT_ROOT/src/main/java/com/example/ytdlp"
JAVAFX_HOME="${JAVAFX_HOME:-/Users/alexlee/Downloads/javafx-sdk-23.0.1}"

# --- Locate a JDK ---------------------------------------------------------
if [[ -z "${JAVA_HOME:-}" ]]; then
  if [[ -x /usr/libexec/java_home ]]; then
    JAVA_HOME="$(/usr/libexec/java_home 2>/dev/null || true)"
  fi
fi
if [[ -z "${JAVA_HOME:-}" || ! -x "$JAVA_HOME/bin/javac" ]]; then
  echo "ERROR: could not find a JDK. Set JAVA_HOME to a JDK 22+ install." >&2
  exit 1
fi
export PATH="$JAVA_HOME/bin:$PATH"

cmd="${1:-build}"

clean() {
  echo "Cleaning target/ and staged runtime files..."
  rm -rf "$PROJECT_ROOT/target/recompile"
  rm -f  "$JAR_DIR/data.json" "$JAR_DIR/cookies.txt" \
         "$JAR_DIR/DownloadMusicAndThumbnails.py" \
         "$JAR_DIR/DownloadThumbnails.py" \
         "$JAR_DIR/DownloadVideos.py"
}

build() {
  if [[ ! -f "$JAR_PATH" ]]; then
    echo "ERROR: $JAR_PATH not found." >&2
    echo "Build it once in IntelliJ (Build > Build Artifacts > YTDLP:jar > Build) and rerun." >&2
    exit 1
  fi

  echo "Compiling Java sources against $JAR_PATH ..."
  mkdir -p "$PROJECT_ROOT/target/recompile"
  # Compile every .java under the package directory; classpath = existing jar (it bundles JavaFX + org.json)
  find "$SRC_PKG_DIR" -maxdepth 1 -name '*.java' -print0 \
    | xargs -0 javac -d "$PROJECT_ROOT/target/recompile" -cp "$JAR_PATH"

  echo "Updating jar with new class files ..."
  cp "$JAR_PATH" "$JAR_PATH.bak"
  (
    cd "$PROJECT_ROOT/target/recompile"
    # shellcheck disable=SC2046
    jar uf "$JAR_PATH" $(find com -name '*.class')
  )

  echo "Staging runtime files next to the jar ..."
  cp "$SRC_PKG_DIR/data.json"                       "$JAR_DIR/data.json"
  cp "$SRC_PKG_DIR/DownloadMusicAndThumbnails.py"   "$JAR_DIR/"
  cp "$SRC_PKG_DIR/DownloadThumbnails.py"           "$JAR_DIR/"
  cp "$SRC_PKG_DIR/DownloadVideos.py"               "$JAR_DIR/"
  [[ -f "$SRC_PKG_DIR/cookies.txt" ]] && cp "$SRC_PKG_DIR/cookies.txt" "$JAR_DIR/" || true

  echo "Build complete: $JAR_PATH"
}

run() {
  if [[ ! -d "$JAVAFX_HOME/lib" ]]; then
    echo "ERROR: JavaFX SDK not found at $JAVAFX_HOME. Set JAVAFX_HOME." >&2
    exit 1
  fi
  echo "Launching YTDLP ..."
  exec java \
    --module-path "$JAVAFX_HOME/lib" \
    --add-modules javafx.controls,javafx.fxml,javafx.graphics \
    -jar "$JAR_PATH"
}

case "$cmd" in
  clean)            clean ;;
  run)              build && run ;;
  build|"")         build ;;
  *)                echo "Unknown command: $cmd"; echo "Usage: $0 [build|run|clean]"; exit 2 ;;
esac
