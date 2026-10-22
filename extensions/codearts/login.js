/* Shared by the dashboard and its behavioral popup regression tests. */
(function(root) {
  'use strict';
  async function start(openWindow, request) {
    const popup = openWindow();
    if (popup) popup.opener = null;
    try {
      const result = await request();
      const url = new URL(result.auth_url);
      if (!['http:', 'https:'].includes(url.protocol) || !result.session_id) {
        throw new Error('Invalid CodeArts authorization URL or session');
      }
      const popupOpened = Boolean(popup && !popup.closed);
      if (popupOpened) popup.location.href = url.href;
      return {sessionId: result.session_id, authUrl: url.href, popupOpened};
    } catch (error) {
      if (popup && !popup.closed) popup.close();
      throw error;
    }
  }
  const api = {start};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CodeArtsLogin = api;
})(typeof window !== 'undefined' ? window : globalThis);
