'use strict';
/** Microphone access belongs only to the trusted Voice page, never arbitrary pages. */
function sameBackendOrigin(url, baseUrl) {
  try {
    const target = new URL(url), base = new URL(baseUrl);
    return ['http:', 'https:'].includes(base.protocol) && target.origin === base.origin;
  } catch { return false; }
}
function voicePage(webContents, requestingUrl, baseUrl) {
  try {
    const page = webContents.getURL();
    return sameBackendOrigin(page, baseUrl) && new URL(page).pathname === '/voice'
      && sameBackendOrigin(requestingUrl, baseUrl);
  } catch { return false; }
}
function configureVoicePermissions(pages, getBaseUrl) {
  pages.setPermissionRequestHandler((webContents, permission, callback, details = {}) => {
    const kinds = details.mediaTypes;
    callback(permission === 'media' && details.isMainFrame !== false
      && voicePage(webContents, details.requestingUrl, getBaseUrl())
      && Array.isArray(kinds) && kinds.length === 1 && kinds[0] === 'audio');
  });
  if (typeof pages.setPermissionCheckHandler === 'function') {
    pages.setPermissionCheckHandler((webContents, permission, origin, details = {}) =>
      permission === 'media' && details.isMainFrame !== false && details.mediaType !== 'video'
      && voicePage(webContents, origin, getBaseUrl()));
  }
}
module.exports = { sameBackendOrigin, configureVoicePermissions };
