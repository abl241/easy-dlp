module com.example.ytdlp {
    requires javafx.controls;
    requires javafx.fxml;

    requires org.kordamp.bootstrapfx.core;
    requires org.json;

    opens com.example.ytdlp to javafx.fxml;
    exports com.example.ytdlp;
}