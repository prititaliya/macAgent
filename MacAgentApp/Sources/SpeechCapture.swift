import Foundation
import Speech
import AVFoundation
import AppKit
import Combine

/// In-overlay dictation via macOS Speech Recognition (same path as typing → `/v1/ask`).
/// FreeFlow remains an optional external STT pipeline; this does not send audio to FreeFlow.
@MainActor
final class SpeechCapture: ObservableObject {
    @Published private(set) var isListening = false
    @Published private(set) var partialText = ""
    @Published var statusMessage: String = ""
    @Published private(set) var speechAuthorized = false
    @Published private(set) var micAuthorized = false

    private let recognizer: SFSpeechRecognizer?
    private let audioEngine = AVAudioEngine()
    private var recognitionRequest: SFSpeechAudioBufferRecognitionRequest?
    private var recognitionTask: SFSpeechRecognitionTask?
    private var finalText = ""

    init(locale: Locale = .current) {
        recognizer = SFSpeechRecognizer(locale: locale)
            ?? SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
        refreshAuthFlags()
    }

    var isSupported: Bool {
        recognizer?.isAvailable == true
    }

    func refreshAuthFlags() {
        speechAuthorized = SFSpeechRecognizer.authorizationStatus() == .authorized
        micAuthorized = Self.microphoneAuthorized()
    }

    /// Toggle listening. Returns a final transcript when stopping (may be empty).
    func toggle() async -> String? {
        if isListening {
            return stop()
        }
        await start()
        return nil
    }

    func start() async {
        statusMessage = ""
        guard !isListening else { return }
        guard isSupported else {
            statusMessage = "Speech recognition isn’t available on this Mac."
            return
        }

        let speechOK = await requestSpeechAuth()
        guard speechOK else {
            statusMessage = "Enable Speech Recognition for MacAgent in System Settings."
            openSpeechSettings()
            return
        }

        let micOK = await requestMicAuth()
        guard micOK else {
            statusMessage = "Enable Microphone for MacAgent in System Settings."
            openMicSettings()
            return
        }

        do {
            try beginRecognition()
            isListening = true
            statusMessage = "Listening… tap mic to stop"
        } catch {
            teardown()
            statusMessage = "Couldn’t start mic: \(error.localizedDescription)"
        }
    }

    @discardableResult
    func stop() -> String {
        let text = (finalText.isEmpty ? partialText : finalText)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        teardown()
        isListening = false
        statusMessage = text.isEmpty ? "No speech captured" : ""
        partialText = ""
        finalText = ""
        return text
    }

    func cancel() {
        teardown()
        isListening = false
        partialText = ""
        finalText = ""
        statusMessage = ""
    }

    // MARK: - Auth

    private func requestSpeechAuth() async -> Bool {
        let status = SFSpeechRecognizer.authorizationStatus()
        if status == .authorized {
            speechAuthorized = true
            return true
        }
        if status == .denied || status == .restricted {
            speechAuthorized = false
            return false
        }
        return await withCheckedContinuation { cont in
            SFSpeechRecognizer.requestAuthorization { newStatus in
                Task { @MainActor in
                    self.speechAuthorized = newStatus == .authorized
                    cont.resume(returning: newStatus == .authorized)
                }
            }
        }
    }

    private func requestMicAuth() async -> Bool {
        if Self.microphoneAuthorized() {
            micAuthorized = true
            return true
        }
        return await withCheckedContinuation { cont in
            if #available(macOS 14.0, *) {
                AVAudioApplication.requestRecordPermission { granted in
                    Task { @MainActor in
                        self.micAuthorized = granted
                        cont.resume(returning: granted)
                    }
                }
            } else {
                AVCaptureDevice.requestAccess(for: .audio) { granted in
                    Task { @MainActor in
                        self.micAuthorized = granted
                        cont.resume(returning: granted)
                    }
                }
            }
        }
    }

    private static func microphoneAuthorized() -> Bool {
        if #available(macOS 14.0, *) {
            return AVAudioApplication.shared.recordPermission == .granted
        }
        return AVCaptureDevice.authorizationStatus(for: .audio) == .authorized
    }

    func openMicSettings() {
        if let url = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
        ) {
            NSWorkspace.shared.open(url)
        }
    }

    func openSpeechSettings() {
        if let url = URL(
            string: "x-apple.systempreferences:com.apple.preference.security?Privacy_SpeechRecognition"
        ) {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: - Engine

    private func beginRecognition() throws {
        teardownEngineOnly()

        guard let recognizer else {
            throw SpeechCaptureError.unavailable
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        // On-device when possible (privacy + works offline for supported locales).
        if recognizer.supportsOnDeviceRecognition {
            request.requiresOnDeviceRecognition = true
        }
        recognitionRequest = request
        partialText = ""
        finalText = ""

        let input = audioEngine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw SpeechCaptureError.noInputDevice
        }

        input.removeTap(onBus: 0)
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self] buffer, _ in
            self?.recognitionRequest?.append(buffer)
        }

        audioEngine.prepare()
        try audioEngine.start()

        recognitionTask = recognizer.recognitionTask(with: request) { [weak self] result, error in
            Task { @MainActor in
                guard let self else { return }
                if let result {
                    let text = result.bestTranscription.formattedString
                    self.partialText = text
                    if result.isFinal {
                        self.finalText = text
                    }
                }
                if error != nil || (result?.isFinal == true) {
                    // Engine may end the task after silence; keep listening UI until user taps stop
                    // unless we already stopped.
                    if let error, self.isListening {
                        let ns = error as NSError
                        // 1110 / no-speech is common after silence — ignore.
                        if ns.domain == "kAFAssistantErrorDomain", ns.code == 1110 {
                            return
                        }
                        if self.finalText.isEmpty && !self.partialText.isEmpty {
                            self.finalText = self.partialText
                        }
                    }
                }
            }
        }
    }

    private func teardown() {
        recognitionRequest?.endAudio()
        teardownEngineOnly()
        recognitionTask?.cancel()
        recognitionTask = nil
        recognitionRequest = nil
    }

    private func teardownEngineOnly() {
        if audioEngine.isRunning {
            audioEngine.stop()
        }
        audioEngine.inputNode.removeTap(onBus: 0)
    }
}

enum SpeechCaptureError: LocalizedError {
    case unavailable
    case noInputDevice

    var errorDescription: String? {
        switch self {
        case .unavailable:
            return "Speech recognition unavailable"
        case .noInputDevice:
            return "No microphone input"
        }
    }
}
