'use strict';
/**
 * Push-to-talk for the desktop shell.
 *
 * The desktop starts listening after a user gesture or in an opted-in conversation. Nothing here
 * starts a timer, watches a page, or retries on its own, so a window left open is never
 * recording by accident. Both capabilities are feature-detected and reported as they
 * are: in Electron the synthesiser's API usually exists while SpeechRecognition usually
 * does not (Chromium's recogniser needs a Google speech service an embedded renderer is
 * not given), so `supported` is false far more often than true. Text is the primary
 * interface and this file never pretends the microphone works.
 *
 * This controller owns the state only while it is listening or talking; a final
 * transcript hands it to the app, which drives the rest through setState().
 */
(function () {
  const STATES = new Set([
    'idle', 'requesting', 'listening', 'transcribing', 'thinking', 'planning',
    'awaiting', 'executing', 'speaking', 'error',
  ]);
  const ERROR_IDLE_DELAY = 2200;
  const UNSUPPORTED_MESSAGE =
    'Speech recognition is not available in this build of Electron. Type your command instead — everything works with text.';
  const ERROR_MESSAGES = {
    'not-allowed': 'The microphone was refused. Allow microphone access for ActionMesh, then try again — or type your command.',
    'service-not-allowed': 'Speech recognition was refused by this build. Type your command instead — everything works with text.',
    'no-speech': 'No speech was detected. Try again.',
    network: 'Speech recognition needs a network connection and could not reach the service.',
    'audio-capture': 'No microphone was found.',
  };

  const NativeRecognition = window.SpeechRecognition || window.webkitSpeechRecognition || null;
  const supported = !window.actionmesh?.transcribeVoice && typeof NativeRecognition === 'function';

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
    idle: 'Idle', requesting: 'Waiting for microphone access', listening: 'Listening', transcribing: 'Transcribing',
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

  let gatewayEnabled = false;
  let recording = null;
  const api = {
    get supported() { return supported || gatewayEnabled; },
    textToSpeechSupported, state: 'idle', ensureLiveRegion, announce,
    setGatewayEnabled(enabled) {
      gatewayEnabled = Boolean(enabled && window.actionmesh?.transcribeVoice
        && window.navigator?.mediaDevices?.getUserMedia && window.MediaRecorder?.isTypeSupported('audio/webm;codecs=opus'));
    },
  };

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

  function releaseRecording(current) {
    clearTimeout(current.timer);
    clearInterval(current.silenceTimer);
    if (current.audioContext) {
      void current.audioContext.close().catch(() => {});
      current.audioContext = null;
    }
    current.stream?.getTracks().forEach((track) => track.stop());
  }

  function stopAfterSpeech(current) {
    if (!window.AudioContext) return;
    const context = new window.AudioContext();
    current.audioContext = context;
    const analyser = context.createAnalyser();
    const source = context.createMediaStreamSource(current.stream);
    source.connect(analyser);
    void context.resume().catch(() => {});
    const samples = new Float32Array(analyser.fftSize);
    let lastSound = 0;
    current.silenceTimer = setInterval(() => {
      analyser.getFloatTimeDomainData(samples);
      const power = samples.reduce((sum, sample) => sum + sample * sample, 0) / samples.length;
      const now = performance.now();
      if (power > 0.000225) {
        lastSound = now;
        current.heardSpeech = true;
      }
      else if (lastSound && now - lastSound >= 1200) stopListening();
    }, 100);
  }

  async function recordUtterance() {
    const current = {};
    recording = current;
    emitState('requesting');
    current.timer = setTimeout(() => {
      if (recording !== current) return;
      recording = null;
      fail('Microphone access did not finish. Check the macOS permission prompt or microphone settings.');
    }, 30000);
    try {
      if (!await window.actionmesh.requestVoiceMic()) {
        if (recording !== current) return;
        const error = new Error();
        error.name = 'NotAllowedError';
        throw error;
      }
      if (recording !== current) return;
      current.stream = await window.navigator.mediaDevices.getUserMedia({ audio: true });
      if (recording !== current) {
        releaseRecording(current);
        return;
      }
      const chunks = [];
      const recorder = new window.MediaRecorder(current.stream, { mimeType: 'audio/webm;codecs=opus' });
      current.recorder = recorder;
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => {
        if (recording !== current) return;
        recording = null;
        releaseRecording(current);
        fail('The microphone recording failed.');
      };
      recorder.onstop = async () => {
        const noSpeech = current.audioContext?.state === 'running' && !current.heardSpeech;
        releaseRecording(current);
        if (recording !== current) return;
        if (noSpeech) {
          recording = null;
          fail(ERROR_MESSAGES['no-speech']);
          return;
        }
        emitState('transcribing');
        try {
          const audio = new Blob(chunks, { type: 'audio/webm' });
          if (!audio.size || audio.size > 1024 * 1024) throw new Error('Record a shorter utterance.');
          const bytes = new Uint8Array(await audio.arrayBuffer());
          if (recording !== current) return;
          const answer = await window.actionmesh.transcribeVoice({ mime: 'audio/webm', bytes });
          if (recording !== current) return;
          if (answer.state !== 'connected') throw new Error(answer.detail || 'Transcription failed.');
          emitState('idle');
          call(onTranscriptFn, answer.transcript, { final: true });
        } catch (error) {
          if (recording === current) fail(error.message || 'Transcription failed.');
        } finally {
          if (recording === current) recording = null;
        }
      };
      recorder.start();
      clearTimeout(current.timer);
      current.timer = setTimeout(stopListening, 15000);
      quietly(() => stopAfterSpeech(current));
      emitState('listening');
    } catch (error) {
      releaseRecording(current);
      if (recording !== current) return;
      recording = null;
      fail(error.name === 'NotAllowedError' ? ERROR_MESSAGES['not-allowed'] : 'The microphone did not start.');
    }
  }

  function startListening() {
    if (recording) return false;
    if (gatewayEnabled && !session) {
      silence();
      void recordUtterance();
      return true;
    }
    if (!supported) {
      fail(window.actionmesh?.transcribeVoice
        ? 'Configure a speech transcription provider in Providers → Audio, then try again.'
        : UNSUPPORTED_MESSAGE);
      return false;
    }

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

    let recognizer;
    try {
      recognizer = new NativeRecognition();
    } catch (error) {
      fail(`Speech recognition could not start: ${error && error.message ? error.message : 'unknown error'}`);
      return false;
    }
    recognizer.continuous = false; // one press, one utterance — never an open mic
    recognizer.interimResults = true;
    recognizer.lang = 'en-US';
    recognizer.maxAlternatives = 1;

    const current = { recognizer, finalDelivered: false };
    session = current;

    recognizer.onresult = (event) => {
      if (session !== current) return;
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const text = (result[0] && result[0].transcript) || '';
        if (!text) continue;
        if (result.isFinal) {
          current.finalDelivered = true;
          call(onTranscriptFn, text.trim(), { final: true });
        } else if (!current.finalDelivered) {
          interim += text;
        }
      }
      // An event can carry interim and final together; the final supersedes the interim
      // from the same event, and emitting the older text afterwards would move the
      // input backwards.
      if (interim && !current.finalDelivered) call(onTranscriptFn, interim.trim(), { final: false });
      // From here the app owns the state machine and drives it with setState().
      if (current.finalDelivered) emitState('transcribing');
    };

    recognizer.onerror = (event) => {
      if (session !== current) return; // an event from a session we already cancelled
      session = null;
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
    if (recording) {
      if (recording.recorder?.state === 'recording') recording.recorder.stop();
      else if (!recording.recorder) cancel();
      return;
    }
    const current = session;
    if (current) quietly(() => current.recognizer.stop());
  }

  /** Stop everything and return to idle. */
  function cancel() {
    if (errorTimer) {
      clearTimeout(errorTimer);
      errorTimer = 0;
    }
    stopSpeaking();
    const captured = recording;
    recording = null;
    if (captured) {
      if (captured.recorder?.state === 'recording') quietly(() => captured.recorder.stop());
      releaseRecording(captured);
    }
    const current = session;
    session = null;
    if (current) quietly(() => current.recognizer.abort()); // abort discards the utterance
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
    speech.lang = 'en-US';
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
      fail(`Speech playback failed (${code}).`);
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

  window.VoiceOSVoice = api;
}());
