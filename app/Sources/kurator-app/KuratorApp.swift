import SwiftUI

/// Делегат нужен ради двух вещей: закрытие последнего окна завершает
/// приложение, а выход приложения гасит python-бэкенд — процесс живёт,
/// пока живо окно.
final class AppDelegate: NSObject, NSApplicationDelegate {
    var model: AppModel?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        model?.shutdown()
    }
}

@main
struct KuratorApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup("kurator") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 900, minHeight: 560)
                .task {
                    delegate.model = model
                    await model.start()
                }
        }
        .windowStyle(.titleBar)
        .defaultSize(width: 1100, height: 760)
    }
}
