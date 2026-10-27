/**
 * Google Apps Script that receives contacts from the pipeline and appends the new ones to an existing tab.
 *
 * Setup, in the Google Sheet:
 *   1. Extensions > Apps Script, and paste this code.
 *   2. Project Settings > Script properties > add TOKEN = a long random string. The same value goes in .env
 *      as GOOGLE_SCRIPT_TOKEN.
 *   3. Deploy > New deployment > Web app. Execute as: Me. Who has access: Anyone.
 *   4. Copy the Web app URL (it ends in /exec) into .env as GOOGLE_SCRIPT_URL.
 * After any change to this code, deploy a new version (Deploy > Manage deployments > Edit > New version).
 *
 * The request is JSON: {token, tab, columns, rows}. The rows go to the tab named in "tab" (GOOGLE_SHEET_TAB
 * in .env), or to the first tab when that is empty. The tab must already exist: this script never creates
 * one. A row whose First Name + Last Name + Crash Date is already in the tab is skipped, so running a
 * stage again never duplicates a person. Values are placed under the tab's own header names, so the tab's column order can differ from the
 * pipeline's, and a column the tab does not have is left out. An empty tab gets the pipeline's header first.
 * New cells are written as plain text, so phone numbers, dates and zip codes are not reformatted.
 */
function doPost(e) {
  var lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    var data = JSON.parse(e.postData.contents);
    var token = PropertiesService.getScriptProperties().getProperty('TOKEN');
    if (!token || data.token !== token) {
      return reply({status: 'error', message: 'bad token'});
    }

    var book = SpreadsheetApp.getActiveSpreadsheet();
    var sheet = data.tab ? book.getSheetByName(data.tab) : book.getSheets()[0];
    if (!sheet) {
      return reply({status: 'error', message: 'tab not found: ' + data.tab});
    }
    var columns = data.columns;

    if (sheet.getLastRow() === 0) {
      sheet.getRange(1, 1, 1, columns.length).setValues([columns]).setFontWeight('bold');
      sheet.setFrozenRows(1);
    }

    var header = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0].map(String);
    var incomingKey = keyColumns(columns);
    var sheetKey = keyColumns(header);

    var known = {};
    if (sheetKey && sheet.getLastRow() > 1) {
      sheet.getRange(2, 1, sheet.getLastRow() - 1, header.length).getValues()
        .forEach(function (row) { known[personKey(row, sheetKey)] = true; });
    }

    var fresh = data.rows.filter(function (row) {
      return !incomingKey || !sheetKey || !known[personKey(row, incomingKey)];
    });
    if (fresh.length) {
      var order = header.map(function (name) { return columns.indexOf(name); });
      var out = fresh.map(function (row) {
        return order.map(function (i) { return i >= 0 ? row[i] : ''; });
      });
      var range = sheet.getRange(sheet.getLastRow() + 1, 1, out.length, header.length);
      range.setNumberFormat('@');
      range.setValues(out);
    }
    return reply({status: 'success', added: fresh.length, skipped: data.rows.length - fresh.length});
  } catch (error) {
    return reply({status: 'error', message: String(error)});
  } finally {
    lock.releaseLock();
  }
}

// A person is identified by First Name + Last Name + Crash Date (the sheet has no id column).
var KEY_NAMES = ['First Name', 'Last Name', 'Crash Date'];

function keyColumns(names) {
  var at = KEY_NAMES.map(function (name) { return names.indexOf(name); });
  return at.indexOf(-1) >= 0 ? null : at;
}

function personKey(row, at) {
  return at.map(function (i) { return String(row[i]).trim().toLowerCase(); }).join('|');
}

function reply(body) {
  return ContentService.createTextOutput(JSON.stringify(body)).setMimeType(ContentService.MimeType.JSON);
}
