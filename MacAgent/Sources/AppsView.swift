import SwiftUI

struct AppsView: View {
    @EnvironmentObject var model: AppModel
    @State private var aliasText = ""
    @State private var appNameText = ""
    @State private var selectedId: Int?
    @State private var busy = false
    @State private var formError: String?

    var body: some View {
        HSplitView {
            List(selection: $selectedId) {
                ForEach(model.apps) { app in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(app.alias)
                            .font(.system(size: 13, weight: .semibold, design: .rounded))
                            .lineLimit(1)
                        Text("→ \(app.app_name)")
                            .font(.system(size: 12, weight: .regular, design: .rounded))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .padding(.vertical, 2)
                    .tag(app.id)
                    .contextMenu {
                        Button("Delete", role: .destructive) {
                            Task { await delete(app.id) }
                        }
                    }
                }
            }
            .listStyle(.sidebar)
            .frame(minWidth: 240)

            VStack(alignment: .leading, spacing: 12) {
                Text(selectedId == nil ? "Add app shortcut" : "Edit app shortcut")
                    .font(.system(size: 13, weight: .semibold, design: .rounded))
                    .foregroundStyle(.secondary)

                TextField("Spoken alias (e.g. vscode, messages)", text: $aliasText)
                    .textFieldStyle(.roundedBorder)
                TextField("macOS app name (e.g. Visual Studio Code)", text: $appNameText)
                    .textFieldStyle(.roundedBorder)

                if let formError {
                    Text(formError)
                        .foregroundStyle(.red)
                        .font(.caption)
                }

                HStack {
                    Button("New") {
                        selectedId = nil
                        aliasText = ""
                        appNameText = ""
                        formError = nil
                    }
                    Spacer()
                    if selectedId != nil {
                        Button("Delete", role: .destructive) {
                            if let id = selectedId {
                                Task { await delete(id) }
                            }
                        }
                        .disabled(busy)
                    }
                    Button(selectedId == nil ? "Add" : "Save") {
                        Task { await save() }
                    }
                    .keyboardShortcut(.defaultAction)
                    .disabled(
                        busy
                            || aliasText.trimmingCharacters(in: .whitespaces).isEmpty
                            || appNameText.trimmingCharacters(in: .whitespaces).isEmpty
                    )
                }

                Text("If the alias already exists, Add updates it. App name must match /Applications (e.g. Google Chrome). For the website, use Sites instead.")
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)

                Spacer()
            }
            .padding(20)
            .frame(minWidth: 320)
        }
        .onChange(of: selectedId) { _ in
            guard let id = selectedId, let app = model.apps.first(where: { $0.id == id }) else { return }
            aliasText = app.alias
            appNameText = app.app_name
            formError = nil
        }
        .task { await model.refreshApps() }
    }

    private func save() async {
        busy = true
        formError = nil
        let alias = aliasText.trimmingCharacters(in: .whitespacesAndNewlines)
        let appName = appNameText.trimmingCharacters(in: .whitespacesAndNewlines)
        do {
            if let id = selectedId {
                try await model.updateApp(id: id, alias: alias, appName: appName)
            } else {
                try await model.addApp(alias: alias, appName: appName)
                selectedId = model.apps.first(where: { $0.alias == alias.lowercased() })?.id
            }
            formError = nil
        } catch {
            formError = "Save failed — check alias/app name"
        }
        busy = false
    }

    private func delete(_ id: Int) async {
        busy = true
        do {
            try await model.deleteApp(id: id)
            if selectedId == id {
                selectedId = nil
                aliasText = ""
                appNameText = ""
            }
        } catch {
            formError = "Delete failed"
        }
        busy = false
    }
}
