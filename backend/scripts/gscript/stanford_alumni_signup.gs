/**
 * LexText Stanford Law Alumni Access - Google Apps Script Backend
 *
 * What this does:
 * 1. Receives the landing-page form POST.
 * 2. Appends the submission to the Google Sheet.
 * 3. Emails support@lextext.ai with the new signup.
 * 4. Sends an optional confirmation email to the applicant's personal email.
 *
 * Deploy:
 * - Open the signup Google Sheet.
 * - Extensions -> Apps Script.
 * - Paste this file into Code.gs.
 * - Deploy -> New deployment -> Web app.
 * - Execute as: Me.
 * - Who has access: Anyone.
 * - Copy the Web app URL.
 * - Replace REPLACE_WITH_DEPLOYED_APPS_SCRIPT_WEB_APP_URL in the HTML form action.
 */

const CONFIG = {
  SHEET_ID: "167RaFCm21BLoVHF79p9yMqvWCdGZliN1syl-4X3gvOY",
  SHEET_NAME: "Signups",
  NOTIFY_EMAIL: "support@lextext.ai",
  FROM_NAME: "LexText Alumni Access",
  THANK_YOU_URL: "https://app.lextext.ai/stanford-alumni-thank-you",
  ACCESS_TERM: "through-2027-06-30",
  PROGRAM: "stanford-law-alumni-2026"
};

function doPost(e) {
  try {
    const params = normalizeParams_(e && e.parameter ? e.parameter : {});
    const validation = validateSubmission_(params);

    if (!validation.ok) {
      return htmlResponse_(
        "LexText Alumni Access",
        "<h1>We could not submit the form.</h1><p>" + escapeHtml_(validation.message) + "</p><p>Please go back and try again, or email support@lextext.ai.</p>"
      );
    }

    const sheet = SpreadsheetApp.openById(CONFIG.SHEET_ID).getSheetByName(CONFIG.SHEET_NAME);
    const timestamp = new Date();

    const row = [
      timestamp,
      "New",
      params.first_name,
      params.last_name,
      params.stanford_email,
      params.personal_email,
      params.graduation_year,
      params.role,
      params.employer,
      params.notes,
      params.consent,
      params.source || "lextext-stanford-alumni-landing-page",
      params.program || CONFIG.PROGRAM,
      params.access_term || CONFIG.ACCESS_TERM,
      userAgent_(e),
      ""
    ];

    sheet.appendRow(row);

    notifyTeam_(params, timestamp);
    sendApplicantConfirmation_(params);

    return redirect_(CONFIG.THANK_YOU_URL);
  } catch (err) {
    console.error(err);
    MailApp.sendEmail({
      to: CONFIG.NOTIFY_EMAIL,
      subject: "[LexText Alumni Access] Form error",
      htmlBody: "<p>A signup submission failed.</p><pre>" + escapeHtml_(String(err && err.stack ? err.stack : err)) + "</pre>"
    });

    return htmlResponse_(
      "LexText Alumni Access",
      "<h1>Something went wrong.</h1><p>Please email support@lextext.ai and we’ll help continue your access.</p>"
    );
  }
}

function normalizeParams_(params) {
  const clean = {};
  Object.keys(params).forEach(function(key) {
    clean[key] = String(params[key] || "").trim();
  });
  return clean;
}

function validateSubmission_(p) {
  const required = ["first_name", "last_name", "stanford_email", "personal_email", "graduation_year", "employer", "consent"];
  for (let i = 0; i < required.length; i++) {
    if (!p[required[i]]) {
      return { ok: false, message: "Missing required field: " + required[i] };
    }
  }

  if (!/@stanford\.edu$/i.test(p.stanford_email)) {
    return { ok: false, message: "Please use your Stanford email address for eligibility verification." };
  }

  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(p.personal_email)) {
    return { ok: false, message: "Please enter a valid personal email address." };
  }

  if (!/^(2024|2025|2026)$/.test(p.graduation_year)) {
    return { ok: false, message: "Please select a valid graduation year." };
  }

  if (p.consent !== "yes") {
    return { ok: false, message: "Consent is required to administer alumni access." };
  }

  return { ok: true };
}

function notifyTeam_(p, timestamp) {
  const subject = "[LexText Alumni Access] New Stanford Law signup: " + p.first_name + " " + p.last_name;
  const html = [
    "<h2>New Stanford Law alumni access request</h2>",
    "<p><b>Name:</b> ", escapeHtml_(p.first_name + " " + p.last_name), "</p>",
    "<p><b>Stanford email:</b> ", escapeHtml_(p.stanford_email), "</p>",
    "<p><b>Personal email:</b> ", escapeHtml_(p.personal_email), "</p>",
    "<p><b>Graduation year:</b> ", escapeHtml_(p.graduation_year), "</p>",
    "<p><b>Next role:</b> ", escapeHtml_(p.role || "Not provided"), "</p>",
    "<p><b>Firm / organization:</b> ", escapeHtml_(p.employer), "</p>",
    "<p><b>Notes:</b><br>", escapeHtml_(p.notes || "None"), "</p>",
    "<p><b>Submitted:</b> ", escapeHtml_(String(timestamp)), "</p>",
    '<p><a href="https://docs.google.com/spreadsheets/d/', CONFIG.SHEET_ID, '/edit">Open signup sheet</a></p>'
  ].join("");

  MailApp.sendEmail({
    to: CONFIG.NOTIFY_EMAIL,
    subject: subject,
    htmlBody: html,
    name: CONFIG.FROM_NAME
  });
}

function sendApplicantConfirmation_(p) {
  const subject = "LexText Stanford Law alumni access request received";
  const html = [
    "<p>Hi ", escapeHtml_(p.first_name), ",</p>",
    "<p>Thanks for requesting continued LexText access as a Stanford Law graduate.</p>",
    "<p>We received your request and will verify eligibility. Once confirmed, we’ll use this personal email to help keep your LexText access active after Stanford SSO or email access expires.</p>",
    "<p>Best,<br>LexText</p>"
  ].join("");

  MailApp.sendEmail({
    to: p.personal_email,
    bcc: CONFIG.NOTIFY_EMAIL,
    subject: subject,
    htmlBody: html,
    name: CONFIG.FROM_NAME
  });
}

function userAgent_(e) {
  try {
    return JSON.stringify(e && e.headers ? e.headers : {});
  } catch (err) {
    return "";
  }
}

function redirect_(url) {
  return HtmlService
    .createHtmlOutput('<!doctype html><meta http-equiv="refresh" content="0; url=' + escapeHtml_(url) + '"><p>Redirecting...</p>')
    .setTitle("LexText Alumni Access");
}

function htmlResponse_(title, body) {
  return HtmlService
    .createHtmlOutput('<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>' + escapeHtml_(title) + '</title><style>body{font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;max-width:680px;margin:8vh auto;padding:0 24px;line-height:1.5;color:#1f2933}a{color:#8C1515}</style></head><body>' + body + '</body></html>')
    .setTitle(title);
}

function escapeHtml_(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
