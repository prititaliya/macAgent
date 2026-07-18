import SwiftUI

struct DebugView: View {
    @EnvironmentObject var model: AppModel
    @State private var selectedId: Int?

    var body: some View {
        HSplitView {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("Traces")
                        .font(.system(size: 13, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button {
                        Task { await model.refreshTraces() }
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                }
                .padding(12)

                List(selection: $selectedId) {
                    ForEach(model.traces) { trace in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(trace.utterance.isEmpty ? "(empty)" : trace.utterance)
                                .font(.system(size: 13, weight: .medium, design: .rounded))
                                .lineLimit(2)
                            HStack {
                                Text("#\(trace.id)")
                                Text(trace.status)
                                Text("\(trace.steps.count) steps")
                            }
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                        }
                        .tag(trace.id)
                    }
                }
            }
            .frame(minWidth: 220)

            ScrollView {
                if let id = selectedId,
                   let trace = model.traces.first(where: { $0.id == id }) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Raw JSON")
                            .font(.system(size: 13, weight: .semibold, design: .rounded))
                            .foregroundStyle(.secondary)
                        Text(prettyJSON(trace.raw))
                            .font(.system(size: 11, design: .monospaced))
                            .textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding(16)
                } else {
                    Text("Select a trace to inspect system prompts, intent JSON, and model I/O.")
                        .foregroundStyle(.secondary)
                        .padding(24)
                }
            }
            .frame(minWidth: 360)
        }
        .task { await model.refreshTraces() }
        .onAppear {
            Task { await model.refreshTraces() }
        }
    }

    private func prettyJSON(_ obj: [String: Any]) -> String {
        guard JSONSerialization.isValidJSONObject(obj),
              let data = try? JSONSerialization.data(
                withJSONObject: obj, options: [.prettyPrinted, .sortedKeys]
              ),
              let text = String(data: data, encoding: .utf8)
        else {
            return String(describing: obj)
        }
        return text
    }
}
