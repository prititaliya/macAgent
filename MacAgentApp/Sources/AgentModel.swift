import Foundation
import AppKit

struct SourceLink: Identifiable, Equatable {
    let id = UUID()
    let title: String
    let url: String
}

struct TraceStep: Identifiable, Equatable {
    let id = UUID()
    let title: String
    let body: String
}

struct DebugTraceItem: Identifiable, Equatable {
    let id: Int
    let utterance: String
    let status: String
    let source: String
    let timestamp: Date?
    let finishedAt: Date?
    let resultSummary: String
    let steps: [DebugTraceStep]
    let raw: [String: Any]

    static func == (lhs: DebugTraceItem, rhs: DebugTraceItem) -> Bool {
        lhs.id == rhs.id
            && lhs.status == rhs.status
            && lhs.steps.count == rhs.steps.count
            && lhs.resultSummary == rhs.resultSummary
    }
}

struct DebugTraceStep: Identifiable, Equatable {
    let id: Int
    let name: String
    let title: String
    let summary: String
    let detail: String
    let isError: Bool
}

struct PendingConfirm: Identifiable, Equatable {
    let id: String
    let summary: String
    let command: String
}

/// Built-in OpenAI-compatible cloud providers (Custom = any other base URL).
enum CloudProviderPreset: String, CaseIterable, Identifiable {
    case openai, deepseek, google, groq, custom

    var id: String { rawValue }

    var label: String {
        switch self {
        case .openai: return "OpenAI"
        case .deepseek: return "DeepSeek"
        case .google: return "Google (Gemini)"
        case .groq: return "Groq"
        case .custom: return "Custom"
        }
    }

    var baseURL: String {
        switch self {
        case .openai: return "https://api.openai.com/v1"
        case .deepseek: return "https://api.deepseek.com/v1"
        case .google: return "https://generativelanguage.googleapis.com/v1beta/openai"
        case .groq: return "https://api.groq.com/openai/v1"
        case .custom: return "https://api.openai.com/v1"
        }
    }

    var defaultModel: String {
        switch self {
        case .openai: return "gpt-4o-mini"
        case .deepseek: return "deepseek-chat"
        case .google: return "gemini-2.0-flash"
        case .groq: return "llama-3.3-70b-versatile"
        case .custom: return "gpt-4o-mini"
        }
    }

    var keyPlaceholder: String {
        switch self {
        case .google: return "Gemini API key"
        case .groq: return "gsk_…"
        case .deepseek: return "sk-…"
        case .openai: return "sk-…"
        case .custom: return "API key"
        }
    }

    static var fallbackCatalog: [[String: Any]] {
        allCases.map {
            [
                "id": $0.rawValue,
                "label": $0.label,
                "base_url": $0.baseURL,
                "model_name": $0.defaultModel,
            ] as [String: Any]
        }
    }

    static func from(id: String) -> CloudProviderPreset {
        CloudProviderPreset(rawValue: id.lowercased()) ?? .custom
    }

    static func infer(fromBaseURL url: String) -> CloudProviderPreset {
        let lower = url.lowercased()
        if lower.contains("deepseek.com") { return .deepseek }
        if lower.contains("groq.com") { return .groq }
        if lower.contains("googleapis.com") || lower.contains("generativelanguage") || lower.contains("gemini") {
            return .google
        }
        if lower.contains("api.openai.com") { return .openai }
        return .custom
    }
}

@MainActor
final class AgentModel: ObservableObject {
    static let shared = AgentModel()

    @Published var answer = ""
    @Published var lastQuestion = ""
    @Published var sources: [SourceLink] = []
    @Published var traceSteps: [TraceStep] = []
    @Published var pendingConfirm: PendingConfirm?
    /// Seconds left before auto-hide; nil when disabled / hidden.
    @Published var hideCountdown: Int?
    /// True only in the last 3 seconds.
    @Published var hideUrgency = false
    /// Alternates only while hideUrgency — drives the heartbeat.
    @Published var hidePulse = false
    @Published var busy = false
    /// True while the overlay mic is recording (pauses auto-hide).
    @Published var isDictating = false
    @Published var statusLine = ""
    @Published var daemonOnline = false
    @Published var history: [[String: Any]] = []
    @Published var sites: [[String: Any]] = []
    @Published var apps: [[String: Any]] = []
    @Published var contextNotes = ""
    @Published var debugTraces: [DebugTraceItem] = []
    @Published var lastError: String?

    /// Full answer text while streaming; `answer` catches up char-by-char.
    private var streamTarget = ""
    private var typewriterTask: Task<Void, Never>?
    private var streamFinalizing = false
    /// True while the typewriter is revealing characters (keeps plain text until done).
    @Published var isStreamingAnswer = false

    /// Follow-up mode: next asks include prior Q&A as conversation context.
    @Published var followUpEnabled = false
    /// Completed turns in the active follow-up thread (oldest → newest).
    @Published var conversationTurns: [(user: String, assistant: String)] = []

    /// Available GGUF models from ~/Models (via daemon).
    @Published var availableModels: [[String: Any]] = []
    @Published var modelDir = ""
    @Published var modelPath = ""
    @Published var modelLoaded = false
    @Published var modelSwitching = false

    /// Short label for the active GGUF (shown under Answer).
    var modelDisplayName: String {
        let raw = modelPath.isEmpty ? "" : URL(fileURLWithPath: modelPath).lastPathComponent
        guard !raw.isEmpty else { return "local model" }
        let stem = raw.replacingOccurrences(of: ".gguf", with: "", options: .caseInsensitive)
        let lower = stem.lowercased()
        if lower.contains("phi-4-mini") || lower.contains("phi4-mini") {
            return "Phi-4-mini"
        }
        if lower.contains("phi-3.5") || lower.contains("phi3.5") {
            return "Phi-3.5 Mini"
        }
        if lower.contains("phi-3") || lower.contains("phi3") {
            return "Phi-3"
        }
        if lower.contains("gemma-4") || lower.contains("gemma4") {
            return "Gemma 4"
        }
        if lower.contains("smollm") || lower.contains("smol-lm") {
            return "SmolLM"
        }
        if lower.contains("llama-3") || lower.contains("llama3") {
            if lower.contains("3.2") || lower.contains("3_2") {
                if lower.contains("1b") { return "Llama 3.2 1B" }
                if lower.contains("3b") { return "Llama 3.2 3B" }
                return "Llama 3.2"
            }
            return "Llama 3"
        }
        if lower.contains("qwen3") || lower.contains("qwen-3") {
            if lower.contains("30b") { return "Qwen3-30B" }
            if lower.contains("4b") { return "Qwen3-4B" }
            return "Qwen3"
        }
        if lower.contains("qwen2.5") || lower.contains("qwen2_5") || lower.contains("qwen-2.5") {
            if lower.contains("7b") { return "Qwen 2.5 7B" }
            return "Qwen 2.5"
        }
        if lower.contains("qwen") {
            return "Qwen"
        }
        return stem
    }

    /// Overlay Model chip label — shows Cloud when acceleration is on.
    var inferenceChipLabel: String {
        if modelSwitching { return "Loading…" }
        if cloudEnabled && cloudApiKeySet {
            let cloud = cloudModelName.trimmingCharacters(in: .whitespacesAndNewlines)
            let short = cloud.isEmpty ? CloudProviderPreset.from(id: cloudProvider).label : cloud
            return "\(modelDisplayName) + \(short)"
        }
        if cloudEnabled {
            return "\(modelDisplayName) · Cloud (no key)"
        }
        return modelDisplayName
    }

    /// Which backend produced the latest answer: "local" or "cloud".
    @Published var answerBackend = "local"

    /// Answer footer attribution — only names cloud when it actually answered.
    var answerAttribution: String {
        if answerBackend == "cloud" && cloudApiKeySet {
            let provider = CloudProviderPreset.from(id: cloudProvider).label
            let cloud = cloudModelName.trimmingCharacters(in: .whitespacesAndNewlines)
            let short = cloud.isEmpty ? provider : cloud
            return "\(provider) · \(short)"
        }
        return modelDisplayName
    }

    var cloudProviderLabel: String {
        CloudProviderPreset.from(id: cloudProvider).label
    }

    /// TTS (Kokoro) prefs from daemon settings.json
    @Published var ttsEnabled = true
    @Published var ttsSpeakStatus = true
    @Published var ttsSpeakAnswer = true
    @Published var ttsVolume: Double = 0.95
    @Published var ttsVoice = "af_heart"
    @Published var ttsMuted = false

    /// Optional OpenAI-compatible cloud acceleration (knowledge answers only).
    @Published var cloudEnabled = false
    @Published var cloudProvider = "openai"
    @Published var cloudBaseURL = "https://api.openai.com/v1"
    @Published var cloudModelName = "gpt-4o-mini"
    @Published var cloudRouteGeneral = true
    @Published var cloudApiKeySet = false
    /// Masked key from daemon (••••xxxx) for display; SecureField draft lives in the prefs pane.
    @Published var cloudApiKeyMasked = ""
    /// Catalog from daemon: [{id, label, base_url, model_name}, …]
    @Published var cloudProviders: [[String: Any]] = CloudProviderPreset.fallbackCatalog

    var onNeedsAttention: (() -> Void)?
    /// Fired when the user interacts or a new answer arrives — resets auto-hide.
    var onUserActivity: (() -> Void)?

    private let base = URL(string: "http://127.0.0.1:8081")!
    private let daemon = DaemonManager()
    private var eventTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?

    private init() {}

    func bootstrap() async {
        statusLine = "Checking local daemon…"
        daemonOnline = await daemon.ensureRunning()
        if daemonOnline {
            statusLine = ""
            lastError = nil
        } else {
            statusLine = "Daemon failed to start"
            lastError = "Check ~/Library/Logs/MacAgent/daemon.err"
        }
        startSSE()
        startPoll()
        await refreshPrefs()
    }

    func shutdown() {
        eventTask?.cancel()
        pollTask?.cancel()
        daemon.stopIfOwned()
    }

    func refreshHealth() async {
        daemonOnline = await daemon.isHealthy()
        if !daemonOnline {
            daemonOnline = await daemon.ensureRunning()
        }
    }

    /// Overlay Search chip: auto | on | off (sent as `use_web` on /v1/ask).
    /// When follow-up is on, prior Q&A turns are sent so the agent continues the thread.
    func ask(_ text: String, useWeb: String = "auto") async {
        let mode: String
        switch useWeb.lowercased() {
        case "on", "off": mode = useWeb.lowercased()
        default: mode = "auto"
        }
        busy = true
        statusLine = "Thinking…"
        lastQuestion = text
        resetAnswerStream()
        answerBackend = "local"
        sources = []
        pendingConfirm = nil
        traceSteps = [
            TraceStep(title: "Input", body: text)
        ]
        lastError = nil
        onUserActivity?()
        defer { busy = false }
        do {
            var req = URLRequest(url: base.appendingPathComponent("v1/ask"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.timeoutInterval = 180
            var payload: [String: Any] = ["text": text, "use_web": mode]
            if followUpEnabled && !conversationTurns.isEmpty {
                payload["follow_up"] = true
                payload["prior_turns"] = conversationTurns.suffix(8).map {
                    ["user": $0.user, "assistant": $0.assistant]
                }
            } else {
                payload["follow_up"] = false
            }
            req.httpBody = try JSONSerialization.data(withJSONObject: payload)
            let (_, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            // Answer arrives via SSE; refresh prefs when done.
            await refreshPrefs()
        } catch {
            lastError = error.localizedDescription
            resetAnswerStream()
            answer = "Request failed: \(error.localizedDescription)"
            statusLine = ""
        }
    }

    /// Start or continue follow-up mode after an answer is available.
    func enableFollowUp() {
        followUpEnabled = true
        commitCurrentTurnIfNeeded()
    }

    /// Clear the thread and turn follow-up off (fresh ask).
    func startNewConversation() {
        followUpEnabled = false
        conversationTurns = []
    }

    /// Append the visible Q&A into the follow-up thread once the answer is final.
    func commitCurrentTurnIfNeeded() {
        let q = lastQuestion.trimmingCharacters(in: .whitespacesAndNewlines)
        let a = answer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !q.isEmpty, !a.isEmpty else { return }
        if let last = conversationTurns.last, last.user == q, last.assistant == a {
            return
        }
        // Replace if same user turn was already recorded with a shorter partial.
        if let last = conversationTurns.last, last.user == q {
            conversationTurns[conversationTurns.count - 1] = (user: q, assistant: a)
        } else {
            conversationTurns.append((user: q, assistant: a))
        }
        if conversationTurns.count > 8 {
            conversationTurns = Array(conversationTurns.suffix(8))
        }
    }

    /// Matches daemon gate in `llm/inference.py` / `main.py` (~5.5 GB+ rejected).
    static let maxModelSizeGB: Double = 5.5

    /// Paths of GGUFs reported by `/v1/models`.
    var modelPaths: [String] {
        availableModels.compactMap { $0["path"] as? String }
    }

    /// Models small enough for reliable local Metal use.
    var usableModelPaths: [String] {
        modelPaths.filter { !isModelTooHeavy($0) }
    }

    /// Models rejected by the daemon size gate (shown disabled in pickers).
    var heavyModelPaths: [String] {
        modelPaths.filter { isModelTooHeavy($0) }
    }

    func sizeGB(for path: String) -> Double? {
        let item = availableModels.first { ($0["path"] as? String) == path }
        if let gb = item?["size_gb"] as? Double { return gb }
        if let gb = item?["size_gb"] as? Int { return Double(gb) }
        if let n = item?["size_gb"] as? NSNumber { return n.doubleValue }
        return nil
    }

    func isModelTooHeavy(_ path: String) -> Bool {
        guard let gb = sizeGB(for: path) else { return false }
        return gb >= Self.maxModelSizeGB
    }

    /// Qwen3-4B Instruct is the recommended local brain for MacAgent.
    func isRecommendedModel(_ path: String) -> Bool {
        let name = URL(fileURLWithPath: path).lastPathComponent.lowercased()
        return name.contains("qwen3") && name.contains("4b") && !isModelTooHeavy(path)
    }

    /// Light GGUFs that fit comfortably on ~8GB unified memory (manual pick).
    func isGoodFor8GBModel(_ path: String) -> Bool {
        guard !isModelTooHeavy(path), !isRecommendedModel(path) else { return false }
        let name = URL(fileURLWithPath: path).lastPathComponent.lowercased()
        if name.contains("llama-3.2") || name.contains("llama3.2") || name.contains("llama-3_2") {
            return name.contains("1b") || name.contains("3b")
        }
        if name.contains("phi-3.5") || name.contains("phi3.5") { return true }
        if name.contains("smollm") || name.contains("smol-lm") { return true }
        if (name.contains("qwen2.5") || name.contains("qwen2_5") || name.contains("qwen-2.5"))
            && name.contains("7b")
        {
            return true
        }
        return false
    }

    /// Menu label for a GGUF path (name + size when known).
    func labelForModelPath(_ path: String) -> String {
        let item = availableModels.first { ($0["path"] as? String) == path }
        let name = item?["name"] as? String
            ?? URL(fileURLWithPath: path).lastPathComponent
        if let gb = sizeGB(for: path) {
            return String(format: "%@ (%.1f GB)", name, gb)
        }
        return name
    }

    /// Picker label with Recommended / Good for 8GB tags.
    func menuLabelForModelPath(_ path: String) -> String {
        let base = labelForModelPath(path)
        if isRecommendedModel(path) {
            return "\(base) · Recommended"
        }
        if isGoodFor8GBModel(path) {
            return "\(base) · Good for 8GB"
        }
        return base
    }

    func respondToConfirm(approve: Bool) async {
        guard let pending = pendingConfirm else { return }
        busy = true
        statusLine = approve ? "Running approved action…" : "Cancelling…"
        onUserActivity?()
        defer { busy = false }
        do {
            var req = URLRequest(url: base.appendingPathComponent("v1/confirm"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.timeoutInterval = 60
            req.httpBody = try JSONSerialization.data(
                withJSONObject: ["id": pending.id, "approve": approve]
            )
            let (_, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            pendingConfirm = nil
            await refreshPrefs()
        } catch {
            lastError = error.localizedDescription
            answer = "Could not send approval: \(error.localizedDescription)"
            statusLine = ""
        }
    }

    func openURL(_ raw: String) {
        guard let url = URL(string: raw) else { return }
        NSWorkspace.shared.open(url)
    }

    func refreshPrefs() async {
        if let obj = await getJSON("v1/activity?limit=40"),
           let items = obj["activity"] as? [[String: Any]] {
            history = items
        }
        if let obj = await getJSON("v1/sites"),
           let items = obj["sites"] as? [[String: Any]] {
            sites = items
        }
        if let obj = await getJSON("v1/apps"),
           let items = obj["apps"] as? [[String: Any]] {
            apps = items
        }
        if let obj = await getJSON("v1/context"),
           let notes = obj["notes"] as? String {
            contextNotes = notes
        }
        if let obj = await getJSON("v1/debug/traces?limit=30"),
           let items = obj["traces"] as? [[String: Any]] {
            debugTraces = items.reversed().compactMap(Self.parseDebugTrace)
        }
        if let obj = await getJSON("v1/settings") {
            if let v = obj["tts_enabled"] as? Bool { ttsEnabled = v }
            if let v = obj["tts_speak_status"] as? Bool { ttsSpeakStatus = v }
            if let v = obj["tts_speak_answer"] as? Bool { ttsSpeakAnswer = v }
            if let v = obj["tts_volume"] as? Double { ttsVolume = v }
            else if let v = obj["tts_volume"] as? NSNumber { ttsVolume = v.doubleValue }
            if let v = obj["tts_voice"] as? String, !v.isEmpty { ttsVoice = v }
            if let v = obj["tts_muted"] as? Bool { ttsMuted = v }
            applyCloudSettings(obj["cloud"] as? [String: Any])
        }
        await refreshModels()
    }

    private func applyCloudSettings(_ cloud: [String: Any]?) {
        guard let cloud else { return }
        if let v = cloud["enabled"] as? Bool { cloudEnabled = v }
        if let v = cloud["provider"] as? String, !v.isEmpty { cloudProvider = v }
        if let v = cloud["base_url"] as? String, !v.isEmpty { cloudBaseURL = v }
        if let v = cloud["model_name"] as? String, !v.isEmpty { cloudModelName = v }
        if let v = cloud["route_general_queries"] as? Bool { cloudRouteGeneral = v }
        if let v = cloud["api_key_set"] as? Bool { cloudApiKeySet = v }
        if let v = cloud["api_key"] as? String { cloudApiKeyMasked = v }
        if let items = cloud["providers"] as? [[String: Any]], !items.isEmpty {
            cloudProviders = items
        }
    }

    func refreshModels() async {
        guard let obj = await getJSON("v1/models") else { return }
        if let dir = obj["model_dir"] as? String { modelDir = dir }
        if let path = obj["model_path"] as? String { modelPath = path }
        if let loaded = obj["model_loaded"] as? Bool { modelLoaded = loaded }
        if let items = obj["models"] as? [[String: Any]] {
            availableModels = items
        }
    }

    /// Switch active GGUF (writes settings.json and reloads in the daemon).
    func selectModel(path: String) async {
        guard !path.isEmpty, path != modelPath else { return }
        modelSwitching = true
        defer { modelSwitching = false }
        statusLine = "Loading model…"
        if let obj = await putJSON(
            "v1/models",
            body: ["model_path": path, "reload": true],
            timeout: 300
        ) {
            if let err = obj["detail"] as? String {
                lastError = err
                statusLine = "Model switch failed"
            } else if let err = obj["error"] as? String {
                lastError = err
                statusLine = "Model switch failed"
            } else {
                lastError = nil
                if let p = obj["model_path"] as? String { modelPath = p }
                if let loaded = obj["model_loaded"] as? Bool { modelLoaded = loaded }
                if let items = obj["models"] as? [[String: Any]] {
                    availableModels = items
                }
                statusLine = modelLoaded ? "Model ready" : "Model selected"
            }
        } else {
            lastError = "Could not switch model — is the daemon running?"
            statusLine = "Model switch failed"
        }
        await refreshModels()
    }

    func saveNotes(_ notes: String) async {
        _ = await putJSON("v1/context", body: ["notes": notes])
        await refreshPrefs()
    }

    func saveTTSSettings(
        enabled: Bool? = nil,
        speakStatus: Bool? = nil,
        speakAnswer: Bool? = nil,
        volume: Double? = nil,
        muted: Bool? = nil
    ) async {
        var body: [String: Any] = [:]
        if let enabled { body["tts_enabled"] = enabled }
        if let speakStatus { body["tts_speak_status"] = speakStatus }
        if let speakAnswer { body["tts_speak_answer"] = speakAnswer }
        if let volume { body["tts_volume"] = volume }
        if let muted { body["tts_muted"] = muted }
        guard !body.isEmpty else { return }
        if let obj = await putJSON("v1/settings", body: body) {
            if let v = obj["tts_enabled"] as? Bool { ttsEnabled = v }
            if let v = obj["tts_speak_status"] as? Bool { ttsSpeakStatus = v }
            if let v = obj["tts_speak_answer"] as? Bool { ttsSpeakAnswer = v }
            if let v = obj["tts_volume"] as? Double { ttsVolume = v }
            else if let v = obj["tts_volume"] as? NSNumber { ttsVolume = v.doubleValue }
            if let v = obj["tts_muted"] as? Bool { ttsMuted = v }
        }
    }

    /// Persist optional cloud acceleration settings.
    /// Omit `apiKey` (or pass nil) to leave the stored key unchanged.
    func saveCloudSettings(
        enabled: Bool? = nil,
        provider: String? = nil,
        baseURL: String? = nil,
        apiKey: String? = nil,
        modelName: String? = nil,
        routeGeneral: Bool? = nil
    ) async {
        var cloud: [String: Any] = [:]
        if let enabled { cloud["enabled"] = enabled }
        if let provider {
            let trimmed = provider.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            if !trimmed.isEmpty { cloud["provider"] = trimmed }
        }
        if let baseURL {
            let trimmed = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { cloud["base_url"] = trimmed }
        }
        if let apiKey {
            let trimmed = apiKey.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty, !trimmed.hasPrefix("••••") {
                cloud["api_key"] = trimmed
            }
        }
        if let modelName {
            let trimmed = modelName.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { cloud["model_name"] = trimmed }
        }
        if let routeGeneral { cloud["route_general_queries"] = routeGeneral }
        guard !cloud.isEmpty else { return }
        if let obj = await putJSON("v1/settings", body: ["cloud": cloud]) {
            applyCloudSettings(obj["cloud"] as? [String: Any])
        }
    }

    func toggleMute() async {
        // Optimistic UI so status speech after STT stops immediately.
        let next = !ttsMuted
        ttsMuted = next
        await saveTTSSettings(muted: next)
    }

    func setDictating(_ active: Bool) async {
        isDictating = active
        _ = await postJSON("v1/tts/dictation", body: ["active": active])
    }

    func addSite(url: String, purpose: String) async {
        _ = await postJSON("v1/sites", body: ["url": url, "purpose": purpose])
        await refreshPrefs()
    }

    func deleteSite(id: Int) async {
        _ = await delete("v1/sites/\(id)")
        await refreshPrefs()
    }

    func addApp(alias: String, appName: String) async {
        _ = await postJSON("v1/apps", body: ["alias": alias, "app_name": appName])
        await refreshPrefs()
    }

    func deleteApp(id: Int) async {
        _ = await delete("v1/apps/\(id)")
        await refreshPrefs()
    }

    // MARK: - private

    private func startSSE() {
        eventTask?.cancel()
        eventTask = Task {
            while !Task.isCancelled {
                do {
                    let url = base.appendingPathComponent("v1/events")
                    let (bytes, _) = try await URLSession.shared.bytes(from: url)
                    for try await line in bytes.lines {
                        if Task.isCancelled { break }
                        guard line.hasPrefix("data: ") else { continue }
                        let payload = String(line.dropFirst(6))
                        guard let data = payload.data(using: .utf8),
                              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
                        else { continue }
                        await MainActor.run { self.handleSSE(obj) }
                    }
                } catch {
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                }
            }
        }
    }

    private func handleSSE(_ obj: [String: Any]) {
        let kind = (obj["kind"] as? String) ?? (obj["type"] as? String) ?? ""
        let text = (obj["text"] as? String)
            ?? (obj["answer"] as? String)
            ?? (obj["message"] as? String)
            ?? ""
        let detail = (obj["detail"] as? String) ?? ""
        if let uttered = obj["utterance"] as? String, !uttered.isEmpty {
            lastQuestion = uttered
            // Start a fresh trace when a new utterance arrives from FreeFlow.
            if kind == "trace", detail == "input" {
                traceSteps = [TraceStep(title: "Input", body: uttered)]
            } else if traceSteps.isEmpty {
                traceSteps = [TraceStep(title: "Input", body: uttered)]
            }
        }

        if kind == "trace" {
            appendTrace(from: obj, fallbackTitle: text)
            busy = true
            if !text.isEmpty { statusLine = text }
            onUserActivity?()
            return
        }

        if kind == "confirm" {
            var confirmId = ""
            var summary = text
            var command = ""
            if let input = obj["tool_input"] as? [String: Any] {
                confirmId = (input["id"] as? String) ?? ""
                if let s = input["summary"] as? String, !s.isEmpty { summary = s }
                command = (input["command"] as? String) ?? ""
            }
            if confirmId.isEmpty {
                if let n = obj["id"] as? Int {
                    confirmId = String(n)
                } else if let s = obj["id"] as? String {
                    confirmId = s
                } else {
                    confirmId = UUID().uuidString
                }
            }
            pendingConfirm = PendingConfirm(
                id: confirmId,
                summary: summary.isEmpty ? "This action needs your permission." : summary,
                command: command
            )
            resetAnswerStream()
            statusLine = "Waiting for your approval…"
            busy = false
            appendTraceLine(title: "Needs permission", body: summary)
            onNeedsAttention?()
            onUserActivity?()
            return
        }

        if kind == "action" || detail == "pending" {
            if !text.isEmpty {
                statusLine = text
                // Keep a short status line in the trace too.
                if text != "Thinking…" && text != "Planning…" {
                    appendTraceLine(title: "Status", body: text)
                }
            }
            busy = true
            onUserActivity?()
            return
        }

        if kind == "answer" || (!text.isEmpty && detail != "pending") {
            let isPartial = detail == "partial"
            if let be = obj["backend"] as? String, !be.isEmpty {
                answerBackend = be
            }
            let (clean, extracted) = Self.stripSources(from: text)
            if !clean.isEmpty {
                if isPartial {
                    enqueueAnswerStream(clean, finalize: false)
                } else {
                    enqueueAnswerStream(clean, finalize: true)
                    appendTraceLine(title: "Answer", body: clean)
                }
            }
            if !extracted.isEmpty {
                sources = extracted
            }
            if isPartial {
                busy = true
                if statusLine.isEmpty { statusLine = "Writing…" }
                onUserActivity?()
                return
            }
            pendingConfirm = nil
            statusLine = ""
            busy = false
            // Only append to the thread when follow-up is explicitly on.
            if followUpEnabled {
                commitCurrentTurnIfNeeded()
            }
            onUserActivity?()
        }

        if let srcs = obj["sources"] as? [[String: Any]] {
            let parsed = srcs.compactMap { s -> SourceLink? in
                guard let url = s["url"] as? String, !url.isEmpty else { return nil }
                return SourceLink(title: (s["title"] as? String) ?? url, url: url)
            }
            if !parsed.isEmpty {
                sources = parsed
            }
        } else if let srcs = obj["sources"] as? [String] {
            let parsed = srcs.compactMap { u -> SourceLink? in
                guard u.hasPrefix("http") else { return nil }
                return SourceLink(title: u, url: u)
            }
            if !parsed.isEmpty {
                sources = parsed
            }
        }

        Task { await refreshPrefs() }
    }

    /// Reset visible answer + typewriter state (new ask / errors / confirm).
    private func resetAnswerStream() {
        typewriterTask?.cancel()
        typewriterTask = nil
        streamTarget = ""
        streamFinalizing = false
        isStreamingAnswer = false
        answer = ""
    }

    /// ChatGPT-style char-by-char reveal toward the latest streamed text.
    private func enqueueAnswerStream(_ full: String, finalize: Bool) {
        streamTarget = full
        streamFinalizing = finalize
        isStreamingAnswer = true
        if finalize && answer.count >= full.count {
            answer = full
            typewriterTask?.cancel()
            typewriterTask = nil
            streamFinalizing = false
            isStreamingAnswer = false
            return
        }
        // If the model rewrote the whole string, restart from empty.
        if !full.hasPrefix(answer) && !answer.isEmpty {
            let common = full.commonPrefix(with: answer)
            answer = common
        }
        guard typewriterTask == nil else { return }
        typewriterTask = Task { @MainActor [weak self] in
            guard let self else { return }
            while !Task.isCancelled {
                let target = self.streamTarget
                if self.answer.count >= target.count {
                    if self.streamFinalizing {
                        self.answer = target
                        self.streamFinalizing = false
                        self.isStreamingAnswer = false
                        break
                    }
                    try? await Task.sleep(nanoseconds: 12_000_000)
                    continue
                }
                if !target.hasPrefix(self.answer) {
                    self.answer = String(target.prefix(self.answer.count))
                    if !target.hasPrefix(self.answer) {
                        self.answer = ""
                    }
                    continue
                }
                let idx = target.index(target.startIndex, offsetBy: self.answer.count)
                self.answer.append(target[idx])
                let ns: UInt64 = self.streamFinalizing ? 2_000_000 : 7_000_000
                try? await Task.sleep(nanoseconds: ns)
            }
            self.typewriterTask = nil
            if self.streamFinalizing || self.answer.count >= self.streamTarget.count {
                self.isStreamingAnswer = false
                self.streamFinalizing = false
            }
        }
    }

    private func appendTrace(from obj: [String: Any], fallbackTitle: String) {
        let step = (obj["step"] as? String) ?? ""
        let tool = (obj["tool"] as? String) ?? ""
        let title: String
        switch step {
        case "input":
            // Never pretty-print {"utterance":…} into the UI — use plain text.
            let spoken = (obj["utterance"] as? String)
                ?? (obj["text"] as? String)
                ?? fallbackTitle
            let clean = spoken == "Received input"
                ? ((obj["utterance"] as? String) ?? lastQuestion)
                : spoken
            if !clean.isEmpty {
                lastQuestion = clean
                appendTraceLine(title: "Input", body: clean)
            }
            return
        case "codegen": title = "Generated code"
        case "shellgen": title = "Generated shell"
        case "tool_call": title = tool.isEmpty ? "Tool call" : "Call \(tool)"
        case "tool_result": title = tool.isEmpty ? "Tool output" : "\(tool) output"
        case "respond": title = "Respond"
        default: title = fallbackTitle.isEmpty ? "Step" : fallbackTitle
        }

        var parts: [String] = []
        if let input = obj["tool_input"] {
            parts.append("IN:\n\(Self.jsonPretty(input))")
        }
        if let output = obj["tool_output"] {
            parts.append("OUT:\n\(Self.jsonPretty(output))")
        }
        if parts.isEmpty, let text = obj["text"] as? String, !text.isEmpty {
            parts.append(text)
        }
        let body = parts.joined(separator: "\n\n")
        guard !body.isEmpty else { return }
        // Avoid duplicating consecutive identical titles with same body
        if let last = traceSteps.last, last.title == title, last.body == body {
            return
        }
        // Replace prior "Input" if we get a fresh input event
        if title == "Input" {
            traceSteps.removeAll { $0.title == "Input" }
        }
        traceSteps.append(TraceStep(title: title, body: body))
    }

    private func appendTraceLine(title: String, body: String) {
        guard !body.isEmpty else { return }
        if let last = traceSteps.last, last.title == title, last.body == body {
            return
        }
        if title == "Answer" || title == "Input" {
            traceSteps.removeAll { $0.title == title }
        }
        traceSteps.append(TraceStep(title: title, body: body))
    }

    private static func jsonPretty(_ value: Any) -> String {
        if let s = value as? String { return s }
        if JSONSerialization.isValidJSONObject(value),
           let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
           let str = String(data: data, encoding: .utf8) {
            return str
        }
        return String(describing: value)
    }

    /// Remove plain-text "Sources:" footer; return clean answer + tappable links.
    private static func stripSources(from text: String) -> (String, [SourceLink]) {
        let markers = ["\n\nSources:\n", "\nSources:\n", "\n\nSources:\r\n"]
        var cutIndex: String.Index?
        for marker in markers {
            if let r = text.range(of: marker) {
                cutIndex = r.lowerBound
                break
            }
        }
        guard let cut = cutIndex else {
            return (text.trimmingCharacters(in: .whitespacesAndNewlines), [])
        }
        let body = String(text[..<cut]).trimmingCharacters(in: .whitespacesAndNewlines)
        let footer = String(text[cut...])
        var found: [SourceLink] = []
        for line in footer.split(separator: "\n").map(String.init) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard trimmed.hasPrefix("- ") else { continue }
            let u = String(trimmed.dropFirst(2)).trimmingCharacters(in: .whitespaces)
            if u.hasPrefix("http") {
                found.append(SourceLink(title: u, url: u))
            }
        }
        return (body, found)
    }

    private func startPoll() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                await refreshHealth()
                try? await Task.sleep(nanoseconds: 5_000_000_000)
            }
        }
    }

    private func getJSON(_ path: String) async -> [String: Any]? {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL else { return nil }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            return try JSONSerialization.jsonObject(with: data) as? [String: Any]
        } catch {
            return nil
        }
    }

    private static func parseDebugTrace(_ item: [String: Any]) -> DebugTraceItem? {
        guard let id = item["id"] as? Int else { return nil }
        let utterance = (item["utterance"] as? String) ?? ""
        let status = (item["status"] as? String) ?? "unknown"
        let source = (item["source"] as? String) ?? ""
        let timestamp = (item["ts"] as? Double).map { Date(timeIntervalSince1970: $0) }
        let finishedAt = (item["finished_ts"] as? Double).map { Date(timeIntervalSince1970: $0) }
        let resultSummary = summarizeDebugValue(item["result"])
        let rawSteps = (item["steps"] as? [[String: Any]]) ?? []
        let steps = rawSteps.enumerated().map { index, step in
            formatDebugStep(index: index, step: step)
        }
        return DebugTraceItem(
            id: id,
            utterance: clip(utterance, 500),
            status: status,
            source: source,
            timestamp: timestamp,
            finishedAt: finishedAt,
            resultSummary: resultSummary,
            steps: steps,
            raw: slimDebugRaw(item)
        )
    }

    private static func formatDebugStep(index: Int, step: [String: Any]) -> DebugTraceStep {
        let name = (step["name"] as? String) ?? "step"
        let isError = name.contains("error") || step["error"] != nil
        let title: String
        switch name {
        case "route": title = "Route"
        case "plan_tool_call": title = "Planner"
        case "plan_tool_call_error": title = "Planner failed"
        case "agent_tool_call": title = "Tool"
        case "final": title = "Result"
        case "answer_from_search", "answer_from_search_error": title = "Search answer"
        case "answer_from_command": title = "Command answer"
        case "intent_heuristic", "intent_llm_error", "intent_error": title = "Intent"
        case "generate_answer": title = "Answer"
        default:
            title = name
                .replacingOccurrences(of: "_", with: " ")
                .capitalized
        }

        var summaryParts: [String] = []
        if let decision = step["decision"] as? String { summaryParts.append(decision) }
        if let useWeb = step["use_web"] as? String { summaryParts.append("search \(useWeb)") }
        if let tool = step["tool"] as? String { summaryParts.append(tool) }
        if let kind = step["kind"] as? String { summaryParts.append(kind) }
        if let backend = step["backend"] as? String { summaryParts.append(backend) }
        if let detail = step["detail"] as? String, !detail.isEmpty {
            summaryParts.append(clip(detail, 80))
        }
        if let error = step["error"] as? String { summaryParts.append(clip(error, 100)) }
        if let text = step["text"] as? String, !text.isEmpty, name == "final" {
            summaryParts.append(clip(text, 100))
        }
        if let raw = step["raw_output"] as? String, !raw.isEmpty, name.contains("plan") || name.contains("answer") {
            summaryParts.append(clip(raw.replacingOccurrences(of: "\n", with: " "), 100))
        }
        if summaryParts.isEmpty {
            let keys = step.keys.filter { !["name", "ts"].contains($0) }.sorted()
            if !keys.isEmpty {
                summaryParts.append(keys.joined(separator: ", "))
            }
        }

        var detailLines: [String] = []
        let preferred = [
            "decision", "use_web", "tool", "args", "step", "kind", "detail", "backend",
            "max_tokens", "text", "error", "raw_output", "command", "user",
            "system_chars", "user_prompt",
        ]
        var seen = Set<String>()
        for key in preferred where step[key] != nil {
            seen.insert(key)
            detailLines.append("\(key): \(stringifyDebugValue(step[key]))")
        }
        for key in step.keys.sorted() where !seen.contains(key) && key != "name" && key != "ts" && key != "system_prompt" {
            detailLines.append("\(key): \(stringifyDebugValue(step[key]))")
        }

        return DebugTraceStep(
            id: index,
            name: name,
            title: title,
            summary: summaryParts.isEmpty ? name : summaryParts.joined(separator: " · "),
            detail: clip(detailLines.joined(separator: "\n\n"), 6000),
            isError: isError
        )
    }

    private static func summarizeDebugValue(_ value: Any?) -> String {
        guard let value else { return "" }
        if let s = value as? String { return clip(s, 160) }
        return clip(stringifyDebugValue(value), 160)
    }

    private static func stringifyDebugValue(_ value: Any?) -> String {
        guard let value else { return "null" }
        if let s = value as? String { return clip(s, 2500) }
        if JSONSerialization.isValidJSONObject(value),
           let data = try? JSONSerialization.data(
            withJSONObject: value,
            options: [.prettyPrinted, .sortedKeys]
           ),
           let text = String(data: data, encoding: .utf8) {
            return clip(text, 2500)
        }
        return clip(String(describing: value), 2500)
    }

    private static func slimDebugRaw(_ item: [String: Any]) -> [String: Any] {
        var slim: [String: Any] = [:]
        for key in ["id", "ts", "finished_ts", "status", "source"] {
            if let value = item[key] { slim[key] = value }
        }
        slim["utterance"] = clip((item["utterance"] as? String) ?? "", 500)
        slim["result"] = clip(stringifyDebugValue(item["result"]), 800)
        if let steps = item["steps"] as? [[String: Any]] {
            slim["steps"] = steps.prefix(40).map { step -> [String: Any] in
                var row: [String: Any] = [:]
                for (k, v) in step {
                    if k == "system_prompt" { continue }
                    if let s = v as? String {
                        row[k] = clip(s, 600)
                    } else {
                        row[k] = v
                    }
                }
                return row
            }
        }
        return slim
    }

    private static func clip(_ text: String, _ max: Int) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.count > max else { return trimmed }
        return String(trimmed.prefix(max - 1)) + "…"
    }

    private func postJSON(_ path: String, body: [String: Any]) async -> [String: Any]? {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return try JSONSerialization.jsonObject(with: data) as? [String: Any]
        } catch {
            return nil
        }
    }

    private func putJSON(
        _ path: String,
        body: [String: Any],
        timeout: TimeInterval = 60
    ) async -> [String: Any]? {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = timeout
        req.httpBody = try? JSONSerialization.data(withJSONObject: body)
        do {
            let (data, _) = try await URLSession.shared.data(for: req)
            return try JSONSerialization.jsonObject(with: data) as? [String: Any]
        } catch {
            return nil
        }
    }

    private func delete(_ path: String) async -> Bool {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL else { return false }
        var req = URLRequest(url: url)
        req.httpMethod = "DELETE"
        do {
            let (_, resp) = try await URLSession.shared.data(for: req)
            return (resp as? HTTPURLResponse).map { (200..<300).contains($0.statusCode) } ?? false
        } catch {
            return false
        }
    }
}
