/**
 * JSW Coil Photos — Google Drive upload endpoint
 * ------------------------------------------------------------------
 * WHAT IT DOES
 *   Receives a coil photo from the JSW Coil OCR app and saves it into a
 *   Drive folder named "JSW Coil Photos" on YOUR Google account.
 *   The manager just opens that folder to review the photos.
 *
 * WHY THIS WAY (no expiring token, you can maintain it alone):
 *   The app POSTs to this script's web-app URL. The script runs AS YOU,
 *   so files land in your own Drive (full 15 GB). The URL never expires.
 *
 * ------------------------------------------------------------------
 * ONE-TIME SETUP (~10 min)
 *   1. Go to  https://script.google.com  ->  New project
 *   2. Delete the sample code, paste THIS whole file, click Save (disk icon).
 *   3. Click  Deploy  ->  New deployment.
 *   4. Gear icon -> choose "Web app".
 *   5. Set:  Execute as = Me      |      Who has access = Anyone
 *   6. Click Deploy. Approve the permissions when Google asks
 *      (it needs permission to create files in your Drive).
 *   7. Copy the "Web app URL" (ends in /exec).
 *   8. In Render -> your service -> Environment, add:
 *        DRIVE_UPLOAD_URL = <that /exec URL>
 *      Save. The app redeploys and starts saving photos.
 *
 * TEST IT:
 *   Open the /exec URL in a browser. You should see {"ok":true,...}.
 *
 * CHANGE THE FOLDER NAME: edit FOLDER_NAME below, Save, then
 *   Deploy -> Manage deployments -> edit -> Version: New version -> Deploy.
 * ------------------------------------------------------------------
 */

var FOLDER_NAME = "JSW Coil Photos";

// Called by the app: saves one photo to Drive.
function doPost(e) {
  try {
    var data  = JSON.parse(e.postData.contents);
    var bytes = Utilities.base64Decode(data.image_b64);
    var blob  = Utilities.newBlob(bytes, "image/jpeg", data.filename);
    getFolder().createFile(blob);
    return json({ ok: true, saved: data.filename });
  } catch (err) {
    return json({ ok: false, error: String(err) });
  }
}

// Open the URL in a browser to confirm the endpoint is alive.
function doGet() {
  return json({ ok: true, msg: "JSW coil photo endpoint is alive" });
}

// Find the target folder, or create it the first time.
function getFolder() {
  var it = DriveApp.getFoldersByName(FOLDER_NAME);
  return it.hasNext() ? it.next() : DriveApp.createFolder(FOLDER_NAME);
}

function json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
