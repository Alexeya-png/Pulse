package app.localtracker.pulse;

import android.app.Activity;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Bundle;
import android.provider.OpenableColumns;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;

public class PulseImportActivity extends Activity {
    private static final int PICK_FILE = 4102;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getSharedPreferences("pulse_import", 0).edit().clear().commit();

        Intent intent = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("*/*");
        intent.putExtra(Intent.EXTRA_MIME_TYPES, new String[] {
                "application/zip",
                "application/json",
                "text/json",
                "application/octet-stream"
        });
        startActivityForResult(intent, PICK_FILE);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);

        if (requestCode != PICK_FILE) return;

        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            getSharedPreferences("pulse_import", 0).edit()
                    .putBoolean("cancelled", true)
                    .commit();
            finish();
            return;
        }

        Uri uri = data.getData();
        String name = displayName(uri);
        String lower = name == null ? "" : name.toLowerCase();

        if (!lower.endsWith(".zip") && !lower.endsWith(".json")) {
            getSharedPreferences("pulse_import", 0).edit()
                    .putString("error", "Выберите ZIP или JSON из выгрузки Instagram.")
                    .commit();
            finish();
            return;
        }

        try {
            File dir = new File(getCacheDir(), "imports");
            if (!dir.exists() && !dir.mkdirs()) {
                throw new Exception("Не удалось создать временную папку.");
            }

            File out = new File(dir, lower.endsWith(".zip") ? "instagram-export.zip" : "instagram-export.json");
            try (InputStream input = getContentResolver().openInputStream(uri);
                 FileOutputStream output = new FileOutputStream(out, false)) {
                if (input == null) throw new Exception("Не удалось открыть выбранный файл.");
                byte[] buffer = new byte[65536];
                int read;
                while ((read = input.read(buffer)) != -1) {
                    output.write(buffer, 0, read);
                }
                output.flush();
            }

            getSharedPreferences("pulse_import", 0).edit()
                    .putBoolean("done", true)
                    .putString("path", out.getAbsolutePath())
                    .commit();
        } catch (Exception exc) {
            getSharedPreferences("pulse_import", 0).edit()
                    .putString("error", "Не удалось прочитать файл.")
                    .commit();
        }

        finish();
    }

    private String displayName(Uri uri) {
        Cursor cursor = null;
        try {
            cursor = getContentResolver().query(uri, null, null, null, null);
            if (cursor != null && cursor.moveToFirst()) {
                int index = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME);
                if (index >= 0) return cursor.getString(index);
            }
        } finally {
            if (cursor != null) cursor.close();
        }
        return uri.getLastPathSegment();
    }

    @Override
    public void onBackPressed() {
        getSharedPreferences("pulse_import", 0).edit()
                .putBoolean("cancelled", true)
                .commit();
        super.onBackPressed();
    }
}
