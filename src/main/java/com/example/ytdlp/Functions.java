package com.example.ytdlp;


import javafx.scene.control.TextField;
import org.json.JSONObject;

import java.io.*;
import java.net.URISyntaxException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.Arrays;
import java.util.List;

public class Functions {

    /**
     * Directory holding the bundled scripts, cookies.txt and data.json.
     * Resolved at startup so the app works regardless of the cwd it was launched from.
     * Order of resolution:
     *   1. -Dytdlp.home=<path> system property
     *   2. The directory containing this class (works when run from source / IDE)
     *   3. The directory containing the running jar (works when packaged)
     *   4. Fallback to the current working directory
     */
    private static final Path APP_DIR = resolveAppDir();

    private static Path resolveAppDir() {
        String override = System.getProperty("ytdlp.home");
        if (override != null && !override.isBlank()) {
            return Paths.get(override).toAbsolutePath();
        }
        try {
            Path classLocation = Paths.get(
                    Functions.class.getProtectionDomain().getCodeSource().getLocation().toURI());
            if (Files.isDirectory(classLocation)) {
                // Running from compiled classes; descend into package directory
                Path pkgDir = classLocation.resolve("com/example/ytdlp");
                if (Files.isDirectory(pkgDir)) return pkgDir;
                return classLocation;
            } else {
                // Running from a jar; use jar's parent directory
                return classLocation.getParent();
            }
        } catch (URISyntaxException | NullPointerException e) {
            return Paths.get("").toAbsolutePath();
        }
    }

    private static Path resourcePath(String name) {
        return APP_DIR.resolve(name);
    }

    public static void ytdlpFunction(TextField textField) {
        writeData("url", textField.getText());

        String dir = retrieveData("dir");
        String url = retrieveData("url");
        System.out.println("Directory: " + dir);
        System.out.println("URL: " + url);

        runPython("DownloadMusicAndThumbnails.py", url, dir);
    }

    public static void ytdlpThumbsFunction(TextField textField) {
        writeData("thumburl", textField.getText());

        String dir = retrieveData("thumbdir");
        String url = retrieveData("thumburl");
        System.out.println("Thumbnail Directory: " + dir);
        System.out.println("Thumbnail URL: " + url);

        runPython("DownloadThumbnails.py", url, dir);
    }

    public static void ytdlpVideosFunction(TextField textField) {
        writeData("url", textField.getText());

        String dir = retrieveData("viddir");
        String url = retrieveData("url");
        System.out.println("Directory: " + dir);
        System.out.println("URL: " + url);

        runPython("DownloadVideos.py", url, dir);
    }

    private static void runPython(String scriptName, String url, String dir) {
        Path script = resourcePath(scriptName);
        ProcessBuilder pb = new ProcessBuilder(
                "python3", script.toString(), url, dir);
        // Run scripts with APP_DIR as cwd so they can find cookies.txt next to them
        pb.directory(APP_DIR.toFile());
        pb.redirectErrorStream(true);
        try {
            Process process = pb.start();
            try (BufferedReader reader = new BufferedReader(
                    new InputStreamReader(process.getInputStream()))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    System.out.println(line);
                }
            }
            process.waitFor();
        } catch (IOException | InterruptedException e) {
            e.printStackTrace();
        }
    }

    public static void embed() {
        String vidDir = retrieveData("embedvideodir");
        String thumbDir = retrieveData("embedthumbdir");
        if (new File(thumbDir).isDirectory()) {
            embedFromFolder();
        } else {
            Path videoPath = Paths.get(vidDir);
            Path output = Paths.get(retrieveData("embedout"))
                    .resolve(videoPath.getFileName());
            runFfmpegEmbed(vidDir, thumbDir, output.toString());
        }
    }

    public static void embedFromFolder() {
        List<String> acceptedFileTypes = Arrays.asList(".jpg", ".jpeg", ".png");
        String thumbDir = retrieveData("embedthumbdir");
        String videoDir = retrieveData("embedvideodir");
        File path = new File(thumbDir);

        File[] files = path.listFiles();
        if (files == null) return;
        for (File file : files) {
            String name = file.getName();
            int dot = name.lastIndexOf('.');
            if (dot == -1) continue;
            if (!acceptedFileTypes.contains(name.substring(dot).toLowerCase())) continue;

            Path thumb = Paths.get(thumbDir, name);
            Path video = Paths.get(videoDir, name.substring(0, dot) + ".mp3");
            System.out.println("Thumb: " + thumb);
            System.out.println("Video: " + video);
            embedFromFolderHelper(video.toString(), thumb.toString());
        }
    }

    public static void embedFromFolderHelper(String vidDir, String thumbDir) {
        Path videoPath = Paths.get(vidDir);
        Path output = Paths.get(retrieveData("embedout"))
                .resolve(videoPath.getFileName());
        runFfmpegEmbed(vidDir, thumbDir, output.toString());
    }

    private static void runFfmpegEmbed(String video, String thumb, String output) {
        String[] command = {
                "ffmpeg",
                "-i", video,
                "-i", thumb,
                "-acodec", "libmp3lame",
                "-b:a", "256k",
                "-c:v", "copy",
                "-map", "0:a:0",
                "-map", "1:v:0",
                output
        };
        ProcessBuilder processBuilder = new ProcessBuilder(command);
        processBuilder.redirectErrorStream(true);
        try {
            Process process = processBuilder.start();
            process.waitFor();
            System.out.println("Process finished successfully.");
        } catch (IOException | InterruptedException e) {
            e.printStackTrace();
        }
    }

    public static String retrieveData(String key) {
        Path dataFile = resourcePath("data.json");
        StringBuilder jsonData = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(new FileReader(dataFile.toFile()))) {
            String line;
            while ((line = reader.readLine()) != null) {
                jsonData.append(line);
            }
            JSONObject jsonObject = jsonData.length() == 0
                    ? new JSONObject()
                    : new JSONObject(jsonData.toString());

            if (jsonObject.has(key)) {
                return jsonObject.get(key).toString();
            }
            return "";
        } catch (FileNotFoundException e) {
            return "";
        } catch (IOException e) {
            e.printStackTrace();
            return "";
        }
    }

    public static void writeData(String key, String value) {
        Path dataFile = resourcePath("data.json");
        JSONObject jsonObject = new JSONObject();
        if (Files.exists(dataFile)) {
            StringBuilder jsonData = new StringBuilder();
            try (BufferedReader reader = new BufferedReader(new FileReader(dataFile.toFile()))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    jsonData.append(line);
                }
                if (jsonData.length() > 0) {
                    jsonObject = new JSONObject(jsonData.toString());
                }
            } catch (IOException e) {
                e.printStackTrace();
            }
        }

        jsonObject.put(key, value);

        try (FileWriter file = new FileWriter(dataFile.toFile())) {
            file.write(jsonObject.toString(4));
        } catch (IOException e) {
            e.printStackTrace();
        }
    }
}
