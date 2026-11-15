package ke.pos.pos_till

import android.app.Activity
import android.content.Intent
import java.io.File
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.embedding.android.FlutterActivity
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private companion object {
        const val CHANNEL = "ke.pos.pos_till/recovery_files"
        const val REQUEST_EXPORT = 7411
        const val REQUEST_IMPORT = 7412
        const val MIME_TYPE = "application/octet-stream"
    }

    private var pendingRecoveryResult: MethodChannel.Result? = null
    private var pendingSourcePath: String? = null
    private var pendingRequestCode: Int? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        MethodChannel(flutterEngine.dartExecutor.binaryMessenger, CHANNEL)
            .setMethodCallHandler { call, result ->
                if (pendingRecoveryResult != null) {
                    result.error("recovery_busy", "A recovery file request is already open.", null)
                    return@setMethodCallHandler
                }

                when (call.method) {
                    "exportEncryptedBackup" -> {
                        val sourcePath = call.argument<String>("sourcePath")
                        val suggestedName = call.argument<String>("suggestedName")
                        if (sourcePath.isNullOrBlank() || suggestedName.isNullOrBlank()) {
                            result.error("invalid_request", "Backup source or name is missing.", null)
                            return@setMethodCallHandler
                        }
                        val source = File(sourcePath)
                        if (!source.isFile) {
                            result.error("backup_missing", "The encrypted till database is missing.", null)
                            return@setMethodCallHandler
                        }

                        pendingRecoveryResult = result
                        pendingSourcePath = sourcePath
                        pendingRequestCode = REQUEST_EXPORT
                        val intent = Intent(Intent.ACTION_CREATE_DOCUMENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            type = MIME_TYPE
                            putExtra(Intent.EXTRA_TITLE, suggestedName)
                        }
                        try {
                            startActivityForResult(intent, REQUEST_EXPORT)
                        } catch (error: Exception) {
                            clearPendingRequest()
                            result.error("file_picker_unavailable", error.message, null)
                        }
                    }

                    "importEncryptedBackup" -> {
                        pendingRecoveryResult = result
                        pendingRequestCode = REQUEST_IMPORT
                        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
                            addCategory(Intent.CATEGORY_OPENABLE)
                            // File providers disagree about the MIME type of
                            // a .sqlite3 document, so let the owner choose and
                            // validate the encrypted contents after copying.
                            type = "*/*"
                        }
                        try {
                            startActivityForResult(intent, REQUEST_IMPORT)
                        } catch (error: Exception) {
                            clearPendingRequest()
                            result.error("file_picker_unavailable", error.message, null)
                        }
                    }

                    else -> result.notImplemented()
                }
            }
    }

    @Deprecated("Deprecated in Android, retained for FlutterActivity compatibility")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != pendingRequestCode) return

        val result = pendingRecoveryResult ?: return
        val uri = data?.data
        val sourcePath = pendingSourcePath
        clearPendingRequest()

        if (resultCode != Activity.RESULT_OK || uri == null) {
            result.success(null)
            return
        }

        Thread {
            try {
                val response: Any = if (requestCode == REQUEST_EXPORT) {
                    if (sourcePath == null) {
                        throw IllegalStateException("The encrypted till database is missing.")
                    }
                    val source = File(sourcePath)
                    val copiedBytes = source.inputStream().use { input ->
                        contentResolver.openOutputStream(uri, "wt").use { output ->
                            if (output == null) {
                                throw IllegalStateException("Could not write the selected backup location.")
                            }
                            val copied = input.copyTo(output, 64 * 1024)
                            output.flush()
                            copied
                        }
                    }
                    if (copiedBytes != source.length()) {
                        throw IllegalStateException("The backup copy was incomplete.")
                    }
                    true
                } else {
                    val imported = File(cacheDir, "pos-till-recovery-import.sqlite3")
                    if (imported.exists() && !imported.delete()) {
                        throw IllegalStateException("Could not prepare a private recovery file.")
                    }
                    val copiedBytes = contentResolver.openInputStream(uri).use { input ->
                        if (input == null) {
                            throw IllegalStateException("Could not read the selected backup.")
                        }
                        imported.outputStream().use { output ->
                            val copied = input.copyTo(output, 64 * 1024)
                            output.flush()
                            copied
                        }
                    }
                    if (copiedBytes != imported.length()) {
                        throw IllegalStateException("The selected backup copy was incomplete.")
                    }
                    imported.absolutePath
                }
                runOnUiThread { result.success(response) }
            } catch (error: Exception) {
                runOnUiThread {
                    result.error("recovery_file_error", error.message, null)
                }
            }
        }.start()
    }

    private fun clearPendingRequest() {
        pendingRecoveryResult = null
        pendingSourcePath = null
        pendingRequestCode = null
    }
}
