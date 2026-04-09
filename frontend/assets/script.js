let mediaRecorder;
let audioChunks = [];
let audioContext;
let analyser;
let microphone;
let silenceTimeout;
let isRecording = false;
let sessionId = null;
let currentQuestionIndex = 0;
let totalQuestions = 5;

const SILENCE_THRESHOLD = 20; // Volume threshold for silence
const SILENCE_DURATION = 2000; // 2 seconds of silence

async function startInterview() {
  try {
    const response = await fetch(`/api/start`);
    const data = await response.json();

    sessionId = data.sessionId;
    currentQuestionIndex = data.questionIndex;
    totalQuestions = data.totalQuestions;

    document.getElementById("startScreen").style.display = "none";
    document.getElementById("interviewScreen").style.display = "block";
    document.getElementById("questionText").textContent = data.question;
    document.getElementById("questionNumber").textContent =
      currentQuestionIndex + 1;
    document.getElementById("totalQuestions").textContent = totalQuestions;

    updateProgress();
  } catch (error) {
    showError("Failed to start interview: " + error.message);
  }
}

async function toggleRecording() {
  if (isRecording) {
    stopRecording();
  } else {
    await startRecording();
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

    audioContext = new (window.AudioContext || window.webkitAudioContext)();
    analyser = audioContext.createAnalyser();
    microphone = audioContext.createMediaStreamSource(stream);
    analyser.fftSize = 512;
    microphone.connect(analyser);

    mediaRecorder = new MediaRecorder(stream);
    audioChunks = [];

    mediaRecorder.ondataavailable = (event) => {
      audioChunks.push(event.data);
    };

    mediaRecorder.onstop = async () => {
      const audioBlob = new Blob(audioChunks, { type: "audio/webm" });
      await sendAudioToBackend(audioBlob);

      stream.getTracks().forEach((track) => track.stop());
      if (audioContext) {
        audioContext.close();
      }
    };

    mediaRecorder.start();
    isRecording = true;

    document.getElementById("recordButton").classList.add("recording");
    document.getElementById("recordButton").textContent = "⏹️";
    document.getElementById("status").textContent = "Recording... (speak now)";
    document.getElementById("status").classList.add("recording");
    document.getElementById("volumeMeter").style.display = "block";
    document.getElementById("responseSection").classList.remove("show");

    detectSilence();
  } catch (error) {
    showError("Failed to access microphone: " + error.message);
  }
}

function detectSilence() {
  if (!isRecording) return;

  const bufferLength = analyser.frequencyBinCount;
  const dataArray = new Uint8Array(bufferLength);
  analyser.getByteFrequencyData(dataArray);

  const average = dataArray.reduce((a, b) => a + b) / bufferLength;

  const volumePercent = Math.min((average / 128) * 100, 100);
  document.getElementById("volumeLevel").style.width = volumePercent + "%";

  if (average < SILENCE_THRESHOLD) {
    if (!silenceTimeout) {
      silenceTimeout = setTimeout(() => {
        if (isRecording) {
          stopRecording();
        }
      }, SILENCE_DURATION);
    }
  } else {
    if (silenceTimeout) {
      clearTimeout(silenceTimeout);
      silenceTimeout = null;
    }
  }

  requestAnimationFrame(detectSilence);
}

function stopRecording() {
  if (!isRecording) return;

  isRecording = false;
  if (silenceTimeout) {
    clearTimeout(silenceTimeout);
    silenceTimeout = null;
  }

  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }

  document.getElementById("recordButton").classList.remove("recording");
  document.getElementById("recordButton").textContent = "🎤";
  document.getElementById("recordButton").disabled = true;
  document.getElementById("status").textContent = "Processing...";
  document.getElementById("status").classList.remove("recording");
  document.getElementById("status").classList.add("processing");
  document.getElementById("volumeMeter").style.display = "none";
}

async function sendAudioToBackend(audioBlob) {
  try {
    const formData = new FormData();
    formData.append("audio", audioBlob, "recording.webm");
    if (sessionId) {
      formData.append("session_id", sessionId);
    }
    console.log(sessionId, formData.get("session_id"));

    const response = await fetch(`/api/answer`, {
      method: "POST",
      body: formData,
    });

    if (!response.ok) {
      throw new Error("Failed to process audio");
    }

    const data = await response.json();
    handleResponse(data);
  } catch (error) {
    showError("Error processing response: " + error.message);
    document.getElementById("recordButton").disabled = false;
    document.getElementById("status").textContent = "Click to speak";
    document.getElementById("status").classList.remove("processing");
  }
}

function handleResponse(data) {
  if (data.sessionId) {
    sessionId = data.sessionId;
  }

  document.getElementById("responseText").textContent = data.response;
  if (data.transcription) {
    document.getElementById("transcriptionText").textContent =
      'You said: "' + data.transcription + '"';
  }
  document.getElementById("responseSection").classList.add("show");

  if (data.nextQuestion) {
    setTimeout(() => {
      document.getElementById("questionText").textContent = data.nextQuestion;
      currentQuestionIndex = data.questionIndex;
      document.getElementById("questionNumber").textContent =
        currentQuestionIndex + 1;
      updateProgress();
    }, 2000);
  }

  if (data.done) {
    document.getElementById("interviewScreen").style.display = "none";
    document.getElementById("completeMessage").classList.add("show");
    updateProgress(100);
    fetchSummary();
    return;
  }

  document.getElementById("recordButton").disabled = false;
  document.getElementById("status").textContent = "Click to speak";
  document.getElementById("status").classList.remove("processing");
}

function updateProgress(percent = null) {
  const progress =
    percent !== null ? percent : (currentQuestionIndex / totalQuestions) * 100;
  document.getElementById("progressFill").style.width = progress + "%";
}

async function fetchSummary() {
  try {
    const response = await fetch(`/api/summary?session_id=${sessionId}`);
    if (!response.ok) throw new Error("Failed to fetch summary");
    const data = await response.json();

    document.querySelector("#completeMessage p").textContent =
      "Thank you for participating. Here is your evaluation:";
    document.getElementById("summaryText").textContent = data.summary;
    document.getElementById("summarySection").style.display = "block";
  } catch (error) {
    document.querySelector("#completeMessage p").textContent =
      "Thank you for participating in the interview.";
  }
}

function showError(message) {
  const errorEl = document.getElementById("errorMessage");
  errorEl.textContent = message;
  errorEl.classList.add("show");
  setTimeout(() => {
    errorEl.classList.remove("show");
  }, 5000);
}
