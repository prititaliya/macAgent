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

struct PendingConfirm: Identifiable, Equatable {
    let id: String
    let summary: String
    let command: String
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
    @Published var debugJSON = "[]"
    @Published var lastError: String?

    var onEvent: (() -> Void)?
    /// Fired when the user interacts or a new answer arrives — resets auto-hide.
    var onUserActivity: (() -> Void)?

    private let base = URL(string: "http://127.0.0.1:8081")!
    private let daemon = DaemonManager()
    private var eventTask: Task<Void, Never>?
    private var pollTask: Task<Void, Never>?

    private init() {}

    func bootstrap() async {
        daemonOnline = await daemon.ensureRunning()
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

    func ask(_ text: String) async {
        busy = true
        statusLine = "Thinking…"
        lastQuestion = text
        answer = ""
        sources = []
        pendingConfirm = nil
        traceSteps = [
            TraceStep(title: "Input", body: text)
        ]
        lastError = nil
        onEvent?()
        onUserActivity?()
        defer { busy = false }
        do {
            var req = URLRequest(url: base.appendingPathComponent("v1/ask"))
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            req.timeoutInterval = 180
            req.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
            let (_, resp) = try await URLSession.shared.data(for: req)
            guard let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            // Answer arrives via SSE; refresh prefs when done.
            await refreshPrefs()
        } catch {
            lastError = error.localizedDescription
            answer = "Request failed: \(error.localizedDescription)"
            statusLine = ""
        }
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
        if let obj = await getJSON("v1/debug/traces?limit=20"),
           let items = obj["traces"] as? [[String: Any]],
           let data = try? JSONSerialization.data(
            withJSONObject: items,
            options: [.prettyPrinted, .sortedKeys]
           ),
           let str = String(data: data, encoding: .utf8) {
            debugJSON = str
        }
    }

    func saveNotes(_ notes: String) async {
        _ = await putJSON("v1/context", body: ["notes": notes])
        await refreshPrefs()
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
            onEvent?()
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
            answer = ""
            statusLine = "Waiting for your approval…"
            busy = false
            appendTraceLine(title: "Needs permission", body: summary)
            onEvent?()
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
            onEvent?()
            onUserActivity?()
            return
        }

        if kind == "answer" || (!text.isEmpty && detail != "pending") {
            let (clean, extracted) = Self.stripSources(from: text)
            if !clean.isEmpty {
                answer = clean
                appendTraceLine(title: "Answer", body: clean)
            }
            if !extracted.isEmpty {
                sources = extracted
            }
            pendingConfirm = nil
            statusLine = ""
            busy = false
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

        onEvent?()
        Task { await refreshPrefs() }
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

    private func putJSON(_ path: String, body: [String: Any]) async -> [String: Any]? {
        guard let url = URL(string: path, relativeTo: base)?.absoluteURL else { return nil }
        var req = URLRequest(url: url)
        req.httpMethod = "PUT"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
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
