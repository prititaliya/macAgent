import Foundation
import Combine

struct AgentEvent: Identifiable, Codable, Equatable {
    let id: Int
    let ts: Double
    let utterance: String
    let kind: String
    let text: String
    let detail: String?

    var isPending: Bool {
        kind == "action" && detail == "pending"
    }
}

struct ActivityRow: Identifiable, Codable, Equatable {
    let id: Int
    let created_at: String?
    let utterance: String
    let action: String
    let detail: String
    let result: String
}

struct PurposeSite: Identifiable, Codable, Equatable {
    let id: Int
    var url: String
    var purpose: String
    var hit_count: Int?
    var last_used: String?
}

struct AppAlias: Identifiable, Codable, Equatable {
    let id: Int
    var alias: String
    var app_name: String
    var hit_count: Int?
    var last_used: String?
}

struct DebugTrace: Identifiable, Equatable {
    let id: Int
    let utterance: String
    let status: String
    let steps: [[String: Any]]
    let raw: [String: Any]

    static func == (lhs: DebugTrace, rhs: DebugTrace) -> Bool {
        lhs.id == rhs.id && lhs.status == rhs.status && lhs.steps.count == rhs.steps.count
    }
}

struct HealthInfo: Equatable {
    var status: String = "unknown"
    var modelPresent: Bool = false
    var modelPath: String = ""
    var modelLoaded: Bool = false
    var purposeSites: Int = 0
}

@MainActor
final class AppModel: ObservableObject {
    @Published var event: AgentEvent?
    @Published var connected: Bool = false
    @Published var statusLine: String = "Starting…"
    @Published var activity: [ActivityRow] = []
    @Published var sites: [PurposeSite] = []
    @Published var apps: [AppAlias] = []
    @Published var traces: [DebugTrace] = []
    @Published var health = HealthInfo()
    @Published var daemonOwnedByApp: Bool = false
    @Published var daemonStarting: Bool = false
    @Published var lastError: String?
    @Published var draftQuery: String = ""
    @Published var sendingQuery: Bool = false
    @Published var userNotes: String = ""
    @Published var contextPreview: String = ""

    var isPending: Bool { event?.isPending == true }

    private let daemon = DaemonManager()
    private var loopTask: Task<Void, Never>?
    private var lastId: Int = 0
    private let baseURL = URL(string: "http://127.0.0.1:8081")!

    init() {
        Task { await bootstrap() }
    }

    deinit {
        // Best-effort; AppModel lives for app lifetime.
    }

    func bootstrap() async {
        daemonStarting = true
        statusLine = "Checking local daemon…"
        let up = await daemon.ensureRunning()
        daemonOwnedByApp = daemon.startedByUs
        daemonStarting = false
        if !up {
            statusLine = "Could not start daemon on :8081"
            lastError = "Check ~/Library/Logs/MacAgent/daemon.err"
            return
        }
        statusLine = "Type below or use FreeFlow Fn…"
        await refreshHealth()
        await refreshActivity()
        await refreshSites()
        await refreshApps()
        await refreshContext()
        await refreshTraces()
        startEventLoop()
    }

    func shutdown() {
        loopTask?.cancel()
        daemon.stopIfOwned()
    }

    func startEventLoop() {
        loopTask?.cancel()
        loopTask = Task { [weak self] in
            while let self, !Task.isCancelled {
                do {
                    try await self.connectSSE()
                } catch {
                    await MainActor.run {
                        self.connected = false
                        self.statusLine = "Daemon offline — retrying…"
                    }
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                    _ = await self.daemon.ensureRunning()
                }
            }
        }
    }

    func refreshHealth() async {
        guard let url = URL(string: "http://127.0.0.1:8081/health") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                health = HealthInfo(
                    status: obj["status"] as? String ?? "ok",
                    modelPresent: obj["model_present"] as? Bool ?? false,
                    modelPath: obj["model_path"] as? String ?? "",
                    modelLoaded: obj["model_loaded"] as? Bool ?? false,
                    purposeSites: obj["purpose_sites"] as? Int ?? 0
                )
            }
        } catch {
            health = HealthInfo(status: "down")
        }
    }

    func refreshActivity() async {
        guard let url = URL(string: "http://127.0.0.1:8081/v1/activity?limit=100") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct Envelope: Codable { let activity: [ActivityRow] }
            activity = try JSONDecoder().decode(Envelope.self, from: data).activity
        } catch {
            // keep previous
        }
    }

    func refreshSites() async {
        guard let url = URL(string: "http://127.0.0.1:8081/v1/sites") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct Envelope: Codable { let sites: [PurposeSite] }
            sites = try JSONDecoder().decode(Envelope.self, from: data).sites
        } catch {
            // keep previous
        }
    }

    func addSite(url: String, purpose: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/sites"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "url": url,
            "purpose": purpose,
        ])
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshSites()
        await refreshHealth()
    }

    func updateSite(id: Int, url: String, purpose: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/sites/\(id)"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "url": url,
            "purpose": purpose,
        ])
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshSites()
    }

    func deleteSite(id: Int) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/sites/\(id)"))
        request.httpMethod = "DELETE"
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshSites()
        await refreshHealth()
    }

    func refreshAll() async {
        await refreshHealth()
        await refreshActivity()
        await refreshSites()
        await refreshApps()
        await refreshContext()
        await refreshTraces()
    }

    func refreshApps() async {
        guard let url = URL(string: "http://127.0.0.1:8081/v1/apps") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            struct Envelope: Codable { let apps: [AppAlias] }
            apps = try JSONDecoder().decode(Envelope.self, from: data).apps
        } catch {
            // keep previous
        }
    }

    func refreshTraces() async {
        guard let url = URL(string: "http://127.0.0.1:8081/v1/debug/traces?limit=40") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let list = obj["traces"] as? [[String: Any]]
            else { return }
            traces = list.reversed().compactMap { item in
                guard let id = item["id"] as? Int else { return nil }
                return DebugTrace(
                    id: id,
                    utterance: item["utterance"] as? String ?? "",
                    status: item["status"] as? String ?? "",
                    steps: item["steps"] as? [[String: Any]] ?? [],
                    raw: item
                )
            }
        } catch {
            // keep previous
        }
    }

    func addApp(alias: String, appName: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/apps"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "alias": alias,
            "app_name": appName,
        ])
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshApps()
    }

    func updateApp(id: Int, alias: String, appName: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/apps/\(id)"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "alias": alias,
            "app_name": appName,
        ])
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshApps()
    }

    func deleteApp(id: Int) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/apps/\(id)"))
        request.httpMethod = "DELETE"
        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        await refreshApps()
    }

    func refreshContext() async {
        guard let url = URL(string: "http://127.0.0.1:8081/v1/context") else { return }
        do {
            let (data, _) = try await URLSession.shared.data(from: url)
            if let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
                userNotes = obj["notes"] as? String ?? ""
                contextPreview = obj["runtime_preview"] as? String ?? ""
            }
        } catch {
            // keep previous
        }
    }

    func saveContext(notes: String) async throws {
        var request = URLRequest(url: baseURL.appendingPathComponent("v1/context"))
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["notes": notes])
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw URLError(.badServerResponse)
        }
        if let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any] {
            userNotes = obj["notes"] as? String ?? notes
            contextPreview = obj["runtime_preview"] as? String ?? ""
        }
    }

    func sendDraft() async {
        let text = draftQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sendingQuery else { return }
        sendingQuery = true
        lastError = nil
        defer { sendingQuery = false }
        do {
            var request = URLRequest(url: baseURL.appendingPathComponent("v1/ask"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.timeoutInterval = 180
            request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text])
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                throw URLError(.badServerResponse)
            }
            // Keep draft so the user can edit and resend.
        } catch {
            lastError = "Send failed — is the daemon running?"
        }
    }

    func loadLastIntoDraft() {
        if let u = event?.utterance, !u.isEmpty {
            draftQuery = u
        }
    }

    private func connectSSE() async throws {
        if let latest = try? await fetchLatestEvents() {
            lastId = latest.map(\.id).max() ?? lastId
            let display = latest.reversed().first(where: { !$0.isPending }) ?? latest.last
            if let display {
                apply(display, force: true)
            }
            connected = true
            statusLine = "Type below or use FreeFlow Fn…"
        }

        var request = URLRequest(url: baseURL.appendingPathComponent("v1/events"))
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        if lastId > 0 {
            var comps = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!
            comps.queryItems = [URLQueryItem(name: "after_id", value: String(lastId))]
            request.url = comps.url
        }

        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw URLError(.badServerResponse)
        }

        connected = true
        statusLine = "Type below or use FreeFlow Fn…"

        let poll = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                guard let self else { return }
                await self.refreshHealth()
                await self.refreshActivity()
                await self.refreshTraces()
                if let latest = try? await self.fetchLatestEvents(),
                   let display = latest.reversed().first(where: { !$0.isPending }) ?? latest.last {
                    self.lastId = max(self.lastId, latest.map(\.id).max() ?? 0)
                    self.apply(display, force: true)
                }
            }
        }
        defer { poll.cancel() }

        var dataBuffer = ""
        for try await line in bytes.lines {
            if Task.isCancelled { break }
            if line.hasPrefix("data:") {
                dataBuffer = String(line.dropFirst(5)).trimmingCharacters(in: .whitespaces)
            } else if line.isEmpty, !dataBuffer.isEmpty {
                let raw = dataBuffer
                dataBuffer = ""
                if let data = raw.data(using: .utf8),
                   let decoded = try? JSONDecoder().decode(AgentEvent.self, from: data) {
                    apply(decoded, force: false)
                    await refreshActivity()
                }
            }
        }
        throw URLError(.networkConnectionLost)
    }

    private func fetchLatestEvents() async throws -> [AgentEvent] {
        let url = baseURL.appendingPathComponent("v1/events/latest")
        let (data, _) = try await URLSession.shared.data(from: url)
        struct Envelope: Codable { let events: [AgentEvent] }
        return try JSONDecoder().decode(Envelope.self, from: data).events
    }

    private func apply(_ event: AgentEvent, force: Bool) {
        if event.isPending {
            if let current = self.event,
               !current.isPending,
               current.utterance.compare(
                event.utterance, options: [.caseInsensitive, .diacriticInsensitive]
               ) == .orderedSame {
                return
            }
            if !force, event.id < lastId { return }
            lastId = max(lastId, event.id)
            self.event = event
            return
        }
        if !force, event.id < lastId, self.event?.isPending != true {
            return
        }
        lastId = max(lastId, event.id)
        self.event = event
    }
}
