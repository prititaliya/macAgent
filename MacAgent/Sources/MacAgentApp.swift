import SwiftUI

@main
struct MacAgentApp: App {
    @StateObject private var model = AppModel()
    @State private var quitObserver: QuitObserver?

    var body: some Scene {
        WindowGroup("MacAgent") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 720, minHeight: 520)
                .onAppear {
                    if quitObserver == nil {
                        quitObserver = QuitObserver(model: model)
                    }
                }
        }
        .defaultSize(width: 820, height: 640)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}
