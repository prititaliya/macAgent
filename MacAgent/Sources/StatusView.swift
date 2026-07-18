import SwiftUI

struct StatusView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Form {
            Section("Daemon") {
                LabeledContent("Health", value: model.health.status)
                LabeledContent("Connected (SSE)", value: model.connected ? "yes" : "no")
                LabeledContent(
                    "Process",
                    value: model.daemonOwnedByApp
                        ? "Started by this app (stops on quit)"
                        : "External / already running"
                )
                if let err = model.lastError {
                    Text(err).foregroundStyle(.red)
                }
            }
            Section("Local model") {
                LabeledContent("Present", value: model.health.modelPresent ? "yes" : "no")
                LabeledContent("Loaded", value: model.health.modelLoaded ? "yes" : "no")
                LabeledContent("Path") {
                    Text(model.health.modelPath.isEmpty ? "—" : model.health.modelPath)
                        .textSelection(.enabled)
                        .font(.system(.body, design: .monospaced))
                }
            }
            Section("Memory") {
                LabeledContent("Purpose sites", value: "\(model.health.purposeSites)")
                LabeledContent("History rows", value: "\(model.activity.count)")
            }
            Section("Context") {
                Text("Date/time is injected automatically. Edit personal notes under Settings.")
                    .foregroundStyle(.secondary)
                if !model.contextPreview.isEmpty {
                    Text(model.contextPreview)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                }
            }
            Section("Voice") {
                Text("FreeFlow Fn → http://127.0.0.1:8081/v1 (local classify / answer / act)")
                    .foregroundStyle(.secondary)
            }
            Section {
                Button("Refresh status") {
                    Task { await model.refreshAll() }
                }
            }
        }
        .formStyle(.grouped)
        .padding()
    }
}
