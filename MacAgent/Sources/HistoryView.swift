import SwiftUI

struct HistoryView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("History")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(.secondary)
                Spacer()
                Text("\(model.activity.count) entries")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 20)
            .padding(.top, 16)
            .padding(.bottom, 8)

            if model.activity.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "clock")
                        .font(.largeTitle)
                        .foregroundStyle(.secondary)
                    Text("No activity yet")
                        .font(.headline)
                    Text("Speak with FreeFlow Fn. Answers and opens show up here.")
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List(model.activity) { row in
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(row.action)
                                .font(.system(size: 11, weight: .semibold, design: .rounded))
                                .foregroundStyle(actionColor(row.action))
                            Spacer()
                            if let when = row.created_at {
                                Text(when)
                                    .font(.caption2)
                                    .foregroundStyle(.tertiary)
                            }
                        }
                        Text(row.utterance.isEmpty ? "(empty)" : row.utterance)
                            .font(.system(size: 14, weight: .medium, design: .rounded))
                        Text(row.result)
                            .font(.system(size: 13, weight: .regular, design: .rounded))
                            .foregroundStyle(.secondary)
                            .lineLimit(4)
                            .textSelection(.enabled)
                    }
                    .padding(.vertical, 4)
                }
                .listStyle(.inset)
            }
        }
    }

    private func actionColor(_ action: String) -> Color {
        switch action {
        case "answer": return .blue
        case "search_fallback", "history", "open_site", "alias_site", "purpose_site":
            return .green
        case "open_app", "alias_app":
            return .green
        default:
            return .secondary
        }
    }
}
