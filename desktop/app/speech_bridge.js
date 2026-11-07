'use strict';
/** Private bridge operations; the Audio credential never leaves Electron main. */
const MAX_WEB_AUDIO_BYTES = 128 * 1024;
function createSpeechHandler({ settings, providers }) {
  let busy = false;
  function config() {
    return { provider: String(settings.get('purpose_audio_provider') || ''),
      model: String(settings.get('purpose_audio_model') || ''),
      baseUrl: String(settings.get('purpose_audio_endpoint') || ''),
      timeoutSeconds: Number(settings.get('purpose_audio_timeout')) || 45,
      apiKey: settings.secret('audioApiKey') || '' };
  }
  return async (request) => {
    const current = config();
    const configured = current.provider === 'router_audio' && Boolean(current.model && current.apiKey)
      && typeof providers.transcribe === 'function';
    if (request.op === 'audio_status') return { ok: true, configured, max_bytes: MAX_WEB_AUDIO_BYTES };
    if (request.op !== 'transcribe') return { ok: false, state: 'malformed', detail: 'Unknown speech operation.' };
    const encoded = request.audio;
    if (Object.keys(request).some((key) => !['token', 'op', 'audio'].includes(key))
        || typeof encoded !== 'string' || !encoded.length || encoded.length > Math.ceil(MAX_WEB_AUDIO_BYTES / 3) * 4
        || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)) {
      return { ok: false, state: 'malformed', detail: 'The recording is invalid or exceeds the web audio limit.' };
    }
    const audio = Buffer.from(encoded, 'base64');
    if (!audio.length || audio.length > MAX_WEB_AUDIO_BYTES || audio.toString('base64') !== encoded) {
      return { ok: false, state: 'malformed', detail: 'The recording is invalid or exceeds the web audio limit.' };
    }
    if (!configured) return { ok: false, state: 'not_configured', detail: 'Configure the Audio provider in the desktop app.' };
    if (busy) return { ok: false, state: 'rate_limited', detail: 'A recording is already being transcribed. Try again shortly.' };
    busy = true;
    try {
      const answer = await providers.transcribe(current, audio);
      if (!answer || !answer.ok || typeof answer.text !== 'string' || answer.text.length > 16000
          || answer.text.includes(current.apiKey)) {
        return { ok: false, state: 'provider_unavailable', detail: 'Cloud transcription failed. Check the Audio provider settings.' };
      }
      return { ok: true, text: answer.text };
    } catch {
      return { ok: false, state: 'provider_unavailable', detail: 'Cloud transcription could not be completed.' };
    } finally { busy = false; }
  };
}
module.exports = { createSpeechHandler, MAX_WEB_AUDIO_BYTES };
