"""The web Voice page's bounded, explicit cloud dictation controller."""

SCRIPT = r"""
(function (global) {
  'use strict';
  const MAX_BYTES = 128 * 1024;
  const MAX_MS = 30000;
  const MIME = 'audio/webm;codecs=opus';
  // Local level detection only: no samples leave this monitor or reach storage.
  function watchSpeechEnd(stream, active, onEnd, onSilence) {
    const Context = global.AudioContext || global.webkitAudioContext;
    if (typeof Context !== 'function') return () => {};
    let context, source, timer = 0, closed = false;
    const cleanup = () => {
      closed = true;
      if (timer) clearTimeout(timer);
      try { if (source) source.disconnect(); } catch { /* already released */ }
      try { if (context) Promise.resolve(context.close()).catch(() => {}); } catch { /* unavailable */ }
    };
    try {
      context = new Context();
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      source = context.createMediaStreamSource(stream);
      source.connect(analyser); // No connection to speakers.
      const samples = new Float32Array(analyser.fftSize);
      let speechFrames = 0, quietFrames = 0, frames = 0, heardSpeech = false;
      const poll = () => {
        if (closed || !active()) { cleanup(); return; }
        try {
          analyser.getFloatTimeDomainData(samples);
          let energy = 0;
          for (const value of samples) energy += value * value;
          const speaking = Math.sqrt(energy / samples.length) > 0.015;
          frames++;
          speechFrames = speaking ? speechFrames + 1 : 0;
          if (speechFrames >= 2) heardSpeech = true;
          quietFrames = speaking ? 0 : quietFrames + 1;
          if (heardSpeech && quietFrames >= 12) { cleanup(); onEnd(); return; }
          if (!heardSpeech && frames >= 100) { cleanup(); onSilence(); return; }
          timer = setTimeout(poll, 80);
        } catch { cleanup(); } // Manual Stop and the hard limit remain available.
      };
      Promise.resolve(context.resume()).then(() => {
        if (!closed && active()) timer = setTimeout(poll, 80);
      }).catch(cleanup);
    } catch { cleanup(); }
    return cleanup;
  }

  function create(options) {
    const supported = Boolean(typeof global.AbortController === 'function'
      && typeof global.Blob === 'function' && typeof global.Blob.prototype.arrayBuffer === 'function'
      && typeof global.btoa === 'function' && global.navigator && global.navigator.mediaDevices
      && typeof global.navigator.mediaDevices.getUserMedia === 'function'
      && global.MediaRecorder && typeof global.MediaRecorder.isTypeSupported === 'function'
      && global.MediaRecorder.isTypeSupported(MIME));
    let current = null;
    const notify = (name, detail) => options.onState(name, detail);
    const release = (session) => {
      if (session.endpointCleanup) { session.endpointCleanup(); session.endpointCleanup = null; }
      if (session.timer) { clearTimeout(session.timer); session.timer = null; }
      if (session.stream) { session.stream.getTracks().forEach((track) => track.stop()); session.stream = null; }
    };
    const cancel = () => {
      const session = current; current = null;
      if (session) {
        session.cancelled = true; release(session);
        if (session.abort) session.abort.abort();
        if (session.recorder && session.recorder.state !== 'inactive') session.recorder.stop();
        session.chunks = [];
      }
      notify('idle', 'Dictation cancelled.');
    };
    const fail = (session, detail) => {
      if (current !== session) return;
      cancel(); notify('error', detail);
    };
    const stop = () => {
      const session = current;
      if (!session) return;
      if (session.recorder && session.recorder.state === 'recording') session.recorder.stop();
      else cancel();
    };
    const start = async () => {
      if (current || !supported) return;
      const session = { chunks: [], bytes: 0, cancelled: false, timer: null, stream: null };
      current = session;
      notify('requesting', 'Checking cloud dictation…');
      try {
        const status = await options.getStatus();
        if (current !== session) return;
        if (!status.configured) { fail(session, 'Configure Audio in the desktop app before using cloud dictation.'); return; }
        const stream = await global.navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        if (current !== session) { stream.getTracks().forEach((track) => track.stop()); return; }
        session.stream = stream;
        const recorder = new global.MediaRecorder(stream, { mimeType: MIME, audioBitsPerSecond: 32000 });
        session.recorder = recorder;
        recorder.ondataavailable = (event) => {
          if (current !== session || !event.data || !event.data.size) return;
          session.bytes += event.data.size;
          if (session.bytes > MAX_BYTES) { fail(session, 'Recording exceeded 128 KiB. Try a shorter utterance.'); return; }
          session.chunks.push(event.data);
        };
        recorder.onerror = () => fail(session, 'Microphone recording failed.');
        recorder.onstop = async () => {
          release(session); // Always stop microphone tracks before any upload.
          if (current !== session) return;
          if (!session.bytes) { fail(session, 'No audio was recorded.'); return; }
          notify('transcribing', 'Microphone off. Transcribing…');
          try {
            const blob = new global.Blob(session.chunks, { type: MIME }); session.chunks = [];
            const bytes = new Uint8Array(await blob.arrayBuffer());
            if (current !== session) return;
            let binary = ''; for (const byte of bytes) binary += String.fromCharCode(byte);
            session.abort = new global.AbortController();
            const answer = await options.transcribe(global.btoa(binary), session.abort.signal);
            if (current !== session) return;
            current = null;
            if (!answer || typeof answer.text !== 'string' || !answer.text.trim()) {
              notify('error', 'No speech was detected.'); return;
            }
            options.onTranscript(answer.text);
            notify('idle', 'Review the transcript, then press Ask VoiceOS. Nothing has run.');
          } catch (error) {
            if (current === session) fail(session, error.message || 'Cloud transcription failed.');
          }
        };
        recorder.start(250);
        session.endpointCleanup = watchSpeechEnd(stream, () => current === session,
          () => { if (recorder.state === 'recording') recorder.stop(); },
          () => fail(session, 'No speech detected. Move closer to the microphone and try again.'));
        session.timer = setTimeout(stop, MAX_MS);
        notify('listening', 'Listening. Pause briefly to finish, or tap Stop (30-second maximum).');
      } catch (error) {
        fail(session, error.name === 'NotAllowedError' ? 'Microphone permission was refused.' : 'The microphone or cloud service could not be reached.');
      }
    };
    return { supported, start, stop, cancel, get active() { return current !== null; } };
  }
  global.ActionMeshCloudVoice = { create, MAX_BYTES, MAX_MS };
})(window);
"""
