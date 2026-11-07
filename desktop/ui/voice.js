'use strict';
/**
 * Push-to-talk for the desktop shell.
 *
 * The microphone opens only when a click handler calls startListening(). Nothing here
 * starts a timer, watches a page, or retries on its own, so a window left open is never
 * recording by accident. Cloud transcription uses the configured Audio lane via
 * the main process, never a credential or a network client in the renderer.
 * Browser-native recognition remains a fallback where the cloud capture APIs are
 * absent; the presence of its constructor does not prove its service is reachable.
 *
 * This controller owns the state only while it is listening or talking; a final
 * transcript hands it to the app, which drives the rest through setState().
 */
(function () {
  const STATES = new Set([
    'idle', 'listening', 'transcribing', 'thinking', 'planning',
    'awaiting', 'executing', 'speaking', 'error',
  ]);
  const ERROR_IDLE_DELAY = 2200;
  const UNSUPPORTED_MESSAGE =
    'Speech recognition is not available in this build of Electron. Type your command instead — everything works with text.';
  const ERROR_MESSAGES = {
    'not-allowed': 'The microphone was refused. Allow microphone access for ActionMesh, then try again — or type your command.',
    'service-not-allowed': 'Speech recognition was refused by this build. Type your command instead — everything works with text.',
    'no-speech': 'No speech was detected. Try again.',
    network: 'The browser speech service could not be reached. This can happen even with working internet; configure the Audio provider in AI Providers for cloud transcription.',
    'audio-capture': 'No microphone was found.',
  };

  const NativeRecognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const cloudSupported = Boolean(window.actionmesh && typeof window.actionmesh.speechTranscribe === 'function'
    && typeof window.actionmesh.speechStatus === 'function'
    && window.navigator && window.navigator.mediaDevices
    && typeof window.navigator.mediaDevices.getUserMedia === 'function'
    && typeof window.MediaRecorder === 'function'
    && typeof window.MediaRecorder.isTypeSupported === 'function'
    && window.MediaRecorder.isTypeSupported('audio/webm;codecs=opus'));
  const supported = cloudSupported || typeof NativeRecognition === 'function';
  const LANGUAGE_KEY = 'actionmesh.voice.input-language';
  const DEFAULT_LANGUAGE = 'en-US';
  const validLanguage = (value) => typeof value === 'string'
    && (value === 'auto' || /^[a-z]{2}-[A-Z]{2}$/.test(value));
  let inputLanguage = DEFAULT_LANGUAGE;
  try {
    const saved = window.localStorage?.getItem(LANGUAGE_KEY);
    if (validLanguage(saved)) inputLanguage = saved;
  } catch {
    // Private storage may be unavailable. The selection still works in this window.
  }

  // Chromium exposes the synthesis API whether or not the platform has a voice
  // installed, and the voice list loads asynchronously — so this flag means exactly
  // "the API is present". A call that cannot actually play reports through onerror;
  // speak() does not claim success beyond having handed the utterance over.
  const Synthesis = typeof window.SpeechSynthesisUtterance === 'function'
    ? window.SpeechSynthesisUtterance
    : null;
  const synthesis = window.speechSynthesis && typeof window.speechSynthesis.speak === 'function'
    ? window.speechSynthesis
    : null;
  const textToSpeechSupported = Boolean(Synthesis && synthesis);

  /** Human-readable labels for screen-reader announcements. Text only. */
  const STATE_LABELS = {
    idle: 'Idle', listening: 'Listening', transcribing: 'Transcribing',
    thinking: 'Thinking', planning: 'Planning', awaiting: 'Waiting for approval',
    executing: 'Executing', speaking: 'Speaking', error: 'Error',
  };

  /**
   * Execution status live region (P2-3 accessibility).
   *
   * voice.js owns no markup (index.html/app.js are lead-owned), so this helper
   * finds the existing status element by id and, only if the page has none,
   * creates a visually-hidden-by-class live region the app can reuse. It never
   * sets inline styles or colours: styling stays in styles.css tokens.
   * Returns the element, or null when no document is available.
   */
  function ensureLiveRegion(doc) {
    const d = doc || (typeof document !== 'undefined' ? document : null);
    if (!d || typeof d.getElementById !== 'function') return null;
    const existing = d.getElementById('voiceState') || d.getElementById('commandStatus');
    const node = existing || (() => {
      const created = d.createElement('div');
      created.setAttribute('id', 'voiceStatusLive');
      created.setAttribute('class', 'voice-status-live');
      created.setAttribute('role', 'status');
      (d.body || d.documentElement || d).append(created);
      return created;
    })();
    node.setAttribute('role', 'status');
    node.setAttribute('aria-live', 'polite');
    node.setAttribute('aria-atomic', 'true');
    return node;
  }

  /** Announce a status change to assistive technology. Text only. */
  function announce(doc, message) {
    const node = ensureLiveRegion(doc);
    if (node) node.textContent = String(message || '');
    return node;
  }

  const api = { supported, cloudSupported, textToSpeechSupported, state: 'idle', ensureLiveRegion, announce };

  // One handler per event, matching the app's single-controller usage; a non-function
  // detaches it. Kept module-level so a listener's own bug can never stop the mic.
  let onTranscriptFn = null;
  let onStateFn = null;
  let onErrorFn = null;

  // The recogniser currently holding the microphone. The object identity lets a late
  // event from an aborted session be recognised as stale.
  let session = null;
  let utterance = null;
  let errorTimer = 0;

  // Handlers are always entered from browser callbacks, so a throw here would be an
  // unhandled error event rather than something the caller can catch. Swallowing it is
  // the only way a renderer bug cannot take the microphone or the speaker down too.
  const quietly = (fn) => { try { fn(); } catch { /* the caller's bug, not ours */ } };
  const call = (handler, ...args) => { if (typeof handler === 'function') quietly(() => handler(...args)); };

  function emitState(state, detail) {
    api.state = state;
    // Screen-reader status: announce every state transition (best-effort;
    // announcement must never take the mic/speaker down).
    quietly(() => announce(null, detail || STATE_LABELS[state] || state));
    call(onStateFn, state, detail);
  }

  /** One failure path: report it, show the error state, then settle back to idle. */
  function fail(message) {
    emitState('error', message);
    call(onErrorFn, message);
    if (errorTimer) clearTimeout(errorTimer);
    errorTimer = setTimeout(() => {
      errorTimer = 0;
      // The app may have moved on while the message was on screen; never stomp it.
      if (api.state === 'error') emitState('idle');
    }, ERROR_IDLE_DELAY);
  }

  /** Cut whatever is being read, without touching state. */
  function silence() {
    utterance = null;
    if (textToSpeechSupported) quietly(() => synthesis.cancel());
  }

  function stopSpeaking() {
    silence();
    if (api.state === 'speaking') emitState('idle');
  }

  function releaseCloud(current) {
    if (current.endpointCleanup) { current.endpointCleanup(); current.endpointCleanup = null; }
    if (current.timer) clearTimeout(current.timer);
    current.timer = 0;
    if (current.stream) current.stream.getTracks().forEach((track) => quietly(() => track.stop()));
    current.stream = null;
  }

  function cancelCloud(current) {
    current.cancelled = true;
    releaseCloud(current);
    if (current.recorder && current.recorder.state !== 'inactive') quietly(() => current.recorder.stop());
    current.chunks = [];
  }

  // Local level detection only: no samples leave this monitor or reach storage.
  function watchSpeechEnd(stream, active, onEnd, onSilence) {
    const Context = window.AudioContext || window.webkitAudioContext;
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

  async function beginCloud(current) {
    try {
      const status = await window.actionmesh.speechStatus();
      if (session !== current) return;
      if (!status || !status.configured) throw new Error('Configure the Audio provider in AI Providers before using the microphone.');
      current.stream = await window.navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
      if (session !== current) { releaseCloud(current); return; }
      for (const track of current.stream.getTracks()) {
        track.addEventListener?.('ended', () => {
          if (session !== current || current.recorder?.state !== 'recording') return;
          session = null;
          cancelCloud(current);
          fail('The microphone disconnected. Reconnect it and try again.');
        }, { once: true });
      }
      const recorder = new window.MediaRecorder(current.stream, { mimeType: 'audio/webm;codecs=opus', audioBitsPerSecond: 32000 });
      current.recorder = recorder;
      recorder.ondataavailable = (event) => {
        if (session !== current || current.cancelled || !event.data || !event.data.size) return;
        current.bytes += event.data.size;
        if (current.bytes > 2 * 1024 * 1024) {
          session = null; cancelCloud(current); fail('The recording exceeded 2 MiB. Try a shorter command.');
          return;
        }
        current.chunks.push(event.data);
      };
      recorder.onerror = () => {
        if (session !== current) return;
        session = null; cancelCloud(current); fail('The microphone recording failed.');
      };
      recorder.onstop = async () => {
        releaseCloud(current);
        if (session !== current || current.cancelled) return;
        emitState('transcribing', 'Sending this recording to your configured Audio provider…');
        try {
          const audio = await new window.Blob(current.chunks, { type: 'audio/webm' }).arrayBuffer();
          current.chunks = [];
          if (session !== current) return;
          const answer = await window.actionmesh.speechTranscribe({ audio, language: current.language });
          if (session !== current) return;
          session = null;
          if (!answer || !answer.ok) { fail((answer && answer.detail) || 'Transcription failed.'); return; }
          if (!answer.text) { fail('No speech was detected. Try again.'); return; }
          emitState('idle');
          call(onTranscriptFn, answer.text, { final: true });
        } catch {
          if (session !== current) return;
          session = null; fail('The transcription request failed. Check the Audio provider settings.');
        }
      };
      recorder.start(250);
      current.endpointCleanup = watchSpeechEnd(current.stream, () => session === current,
        () => { if (recorder.state === 'recording') recorder.stop(); },
        () => { if (session !== current) return; session = null; cancelCloud(current); fail('No speech detected. Move closer to the microphone and try again.'); });
      current.timer = setTimeout(() => {
        if (session === current && recorder.state === 'recording') recorder.stop();
      }, 30000);
      emitState('listening', 'Listening. Pause briefly to finish, or press Stop (30-second maximum).');
    } catch (error) {
      releaseCloud(current);
      if (session !== current) return;
      session = null;
      fail(error && error.name === 'NotAllowedError' ? ERROR_MESSAGES['not-allowed']
        : error && error.name === 'NotFoundError' ? ERROR_MESSAGES['audio-capture']
          : 'Could not start recording. Configure Audio in Settings and allow microphone access.');
    }
  }

  function startListening() {
    if (!supported) {
      fail(UNSUPPORTED_MESSAGE);
      return false;
    }

    if (session && session.cloud) return true;

    if (session) {
      // A second press while the mic is open must not open a second one.
      if (!session.finalDelivered) return true;
      // That utterance already went to the app; this press begins a new one. Abort the
      // old recogniser so it cannot deliver a duplicate final.
      const stale = session;
      session = null;
      quietly(() => stale.recognizer.abort());
    }

    // Never listen while talking: the microphone would transcribe our own reply.
    silence();
    if (cloudSupported) {
      const current = { cloud: true, chunks: [], bytes: 0, timer: 0, stream: null, recorder: null,
        cancelled: false, language: inputLanguage === 'auto' ? '' : inputLanguage.slice(0, 2) };
      session = current;
      emitState('transcribing', 'Checking Audio configuration and microphone permission…');
      beginCloud(current);
      return true;
    }

    let recognizer;
    try {
      recognizer = new NativeRecognition();
    } catch (error) {
      fail(`Speech recognition could not start: ${error && error.message ? error.message : 'unknown error'}`);
      return false;
    }
    recognizer.continuous = false; // one press, one utterance — never an open mic
    recognizer.interimResults = true;
    recognizer.lang = inputLanguage === 'auto'
      ? window.navigator?.language || DEFAULT_LANGUAGE : inputLanguage;
    recognizer.maxAlternatives = 1;

    const current = { recognizer, finalDelivered: false };
    session = current;

    recognizer.onresult = (event) => {
      if (session !== current || current.finalDelivered) return;
      let interim = '';
      const final = [];
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const text = (result[0] && result[0].transcript) || '';
        if (!text) continue;
        if (result.isFinal) {
          final.push(text.trim());
        } else {
          interim += text;
        }
      }
      // An event can carry interim and final together; the final supersedes the interim
      // from the same event, and emitting the older text afterwards would move the
      // input backwards.
      if (final.length) {
        current.finalDelivered = true;
        emitState('transcribing');
        call(onTranscriptFn, final.join(' '), { final: true });
      } else if (interim) {
        call(onTranscriptFn, interim.trim(), { final: false });
      }
    };

    recognizer.onerror = (event) => {
      if (session !== current) return; // an event from a session we already cancelled
      session = null;
      quietly(() => current.recognizer.abort());
      const code = event.error || 'unknown';
      fail(ERROR_MESSAGES[code] || String(code));
    };

    recognizer.onend = () => {
      if (session !== current) return;
      session = null;
      // A final transcript handed the button to the app; an error has its own reset.
      if (!current.finalDelivered) emitState('idle');
    };

    try {
      recognizer.start();
    } catch (error) {
      session = null;
      fail(`The microphone did not start: ${error && error.message ? error.message : 'unknown error'}`);
      return false;
    }

    emitState('listening');
    return true;
  }

  /** Stop after this utterance: unlike abort(), stop() still delivers the final result. */
  function stopListening() {
    const current = session;
    if (current && current.cloud) {
      if (current.recorder && current.recorder.state === 'recording') current.recorder.stop();
      else if (!current.recorder) { session = null; cancelCloud(current); emitState('idle'); }
    } else if (current) quietly(() => current.recognizer.stop());
  }

  /** Stop everything and return to idle. */
  function cancel() {
    if (errorTimer) {
      clearTimeout(errorTimer);
      errorTimer = 0;
    }
    stopSpeaking();
    const current = session;
    session = null;
    if (current && current.cloud) cancelCloud(current);
    else if (current) quietly(() => current.recognizer.abort()); // abort discards the utterance
    if (api.state !== 'idle') emitState('idle');
  }

  function speak(text) {
    if (!textToSpeechSupported) return false;
    const phrase = String(text === undefined || text === null ? '' : text).trim();
    if (!phrase) return false;

    // One reply at a time. A queue of stale answers read after the moment passed is
    // worse than cutting the previous sentence. No idle in between, so no flicker.
    silence();

    const speech = new Synthesis(phrase);
    speech.lang = /[\u0900-\u097f]/.test(phrase) ? 'hi-IN'
      : (inputLanguage.startsWith('en-') ? inputLanguage : DEFAULT_LANGUAGE);
    const voices = typeof synthesis.getVoices === 'function' ? synthesis.getVoices() : [];
    const language = speech.lang.toLowerCase();
    const local = voices.filter(voice => voice.localService !== false);
    const chosen = local.find(voice => String(voice.lang).toLowerCase() === language)
      || local.find(voice => String(voice.lang).toLowerCase().split('-')[0] === language.split('-')[0]);
    if (chosen) speech.voice = chosen;
    speech.rate = 1.0;
    utterance = speech;

    speech.onend = () => {
      if (utterance !== speech) return;
      utterance = null;
      // Only if speaking still owns the state: the app may have moved on meanwhile.
      if (api.state === 'speaking') emitState('idle');
    };

    speech.onerror = (event) => {
      if (utterance !== speech) return; // released by our own silence()
      utterance = null;
      const code = event.error || 'unknown';
      if (code === 'canceled' || code === 'interrupted') {
        // What our own cancel() produces when it was not released first; not a failure.
        if (api.state === 'speaking') emitState('idle');
        return;
      }
      fail(code === 'synthesis-failed'
        ? 'The local speech service could not play this reply. The text answer is still available.'
        : `Speech playback failed (${code}).`);
    };

    try {
      synthesis.speak(speech);
    } catch (error) {
      utterance = null;
      fail(`Speech playback could not start: ${error && error.message ? error.message : 'unknown error'}`);
      return false;
    }
    emitState('speaking', phrase);
    return true;
  }

  // The app drives the button through backend work. Unknown names are ignored: `state`
  // is documented as one of the nine above, and a typo must not leave the button
  // showing something it cannot render.
  function setState(name, detail) {
    if (STATES.has(name)) emitState(name, detail);
  }

  api.startListening = startListening;
  api.stopListening = stopListening;
  api.cancel = cancel;
  api.speak = speak;
  api.stopSpeaking = stopSpeaking;
  api.onTranscript = (fn) => { onTranscriptFn = typeof fn === 'function' ? fn : null; };
  api.onStateChange = (fn) => { onStateFn = typeof fn === 'function' ? fn : null; };
  api.onError = (fn) => { onErrorFn = typeof fn === 'function' ? fn : null; };
  api.setState = setState;
  api.getLanguage = () => inputLanguage;
  api.setLanguage = (value) => {
    if (!validLanguage(value)) return;
    inputLanguage = value;
    try { window.localStorage?.setItem(LANGUAGE_KEY, value); } catch { /* window-only */ }
  };

  window.VoiceOSVoice = api;
}());
