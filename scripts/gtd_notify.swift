import AppKit
import UserNotifications
import WebKit
import Darwin

// A small resident app gives macOS a stable notification and activation identity.
final class Notifications: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let center = UNUserNotificationCenter.current()
    let root: URL
    var source: DispatchSourceFileSystemObject?
    var busy = Set<String>()
    var checkingAuthorization = false
    var window: NSWindow?
    var webView: WKWebView?
    var startingService = false
    var pendingURL: URL?
    var recoveringNavigation = false
    var verifyDirectory: URL?
    var verificationDone = false
    var expectedVerificationView = "today"

    init(root: URL) { self.root = root; super.init() }

    func applicationDidFinishLaunching(_ notification: Notification) {
        center.delegate = self
        let identity: [String: Any] = ["pid": ProcessInfo.processInfo.processIdentifier, "bundle": Bundle.main.bundleURL.path]
        if let data = try? JSONSerialization.data(withJSONObject: identity) {
            try? data.write(to: root.appendingPathComponent("notification-app.json"), options: .atomic)
        }
        let folder = root.appendingPathComponent("notifications", isDirectory: true)
        try? FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let fd = Darwin.open(folder.path, O_EVTONLY)
        if fd >= 0 {
            let watcher = DispatchSource.makeFileSystemObjectSource(fileDescriptor: fd, eventMask: .write, queue: .main)
            watcher.setEventHandler { [weak self] in self?.drain() }
            watcher.setCancelHandler { Darwin.close(fd) }
            source = watcher
            watcher.resume()
        }
        drain()
        installMenus()
        if let index = CommandLine.arguments.firstIndex(of: "--verify-dir"), index + 1 < CommandLine.arguments.count {
            verifyDirectory = URL(fileURLWithPath: CommandLine.arguments[index + 1])
        }
        if !CommandLine.arguments.contains("--background") { showWindow() }
    }

    func config() -> [String: Any]? {
        guard let data = try? Data(contentsOf: root.appendingPathComponent("workbench.json")) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    func workbenchURL(view: String = "today") -> URL? {
        guard let value = config(), let port = value["port"] as? Int, let token = value["token"] as? String else { return nil }
        let selected = ["today", "inbox", "projects", "review"].contains(view) ? view : "today"
        return URL(string: "http://127.0.0.1:\(port)/#access=\(token)&view=\(selected)")
    }

    func isWorkbench(_ url: URL) -> Bool {
        url.scheme == "http" && url.host == "127.0.0.1" && url.port == config()?["port"] as? Int
    }

    func installMenus() {
        let bar = NSMenu(), application = NSMenuItem(), appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于Getting Things Done", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "隐藏Getting Things Done", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "退出Getting Things Done", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        application.submenu = appMenu; bar.addItem(application)
        let editItem = NSMenuItem(), edit = NSMenu(title: "编辑")
        for (title, selector, shortcut) in [("撤销", "undo:", "z"), ("剪切", "cut:", "x"), ("拷贝", "copy:", "c"), ("粘贴", "paste:", "v"), ("全选", "selectAll:", "a")] {
            edit.addItem(withTitle: title, action: NSSelectorFromString(selector), keyEquivalent: shortcut)
        }
        editItem.submenu = edit; bar.addItem(editItem)
        let viewItem = NSMenuItem(), view = NSMenu(title: "显示")
        let reload = NSMenuItem(title: "刷新工作台", action: #selector(reloadPage), keyEquivalent: "r")
        reload.target = self; view.addItem(reload)
        viewItem.submenu = view; bar.addItem(viewItem)
        NSApplication.shared.mainMenu = bar
    }

    @objc func reloadPage() { webView?.reloadFromOrigin(nil) }

    func showWindow(url requested: URL? = nil) {
        if window == nil {
            let bounds = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
            let size = NSSize(width: min(1240, bounds.width - 50), height: min(880, bounds.height - 55))
            let window = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Getting Things Done"
            window.minSize = NSSize(width: 760, height: 580)
            window.isReleasedWhenClosed = false
            window.backgroundColor = NSColor(calibratedRed: 0.97, green: 0.975, blue: 0.955, alpha: 1)
            window.center()
            let settings = WKWebViewConfiguration()
            settings.userContentController.add(self, name: "gtdClipboard")
            let web = WKWebView(frame: .zero, configuration: settings)
            web.navigationDelegate = self; web.uiDelegate = self
            web.allowsBackForwardNavigationGestures = false
            web.autoresizingMask = [.width, .height]
            window.contentView = web
            self.window = window; self.webView = web
        }
        NSApplication.shared.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
        if let requested = requested, isWorkbench(requested) { ensureServiceAndLoad(target: requested) }
        else { ensureServiceAndLoad(target: webView?.url == nil ? workbenchURL() : nil) }
    }

    func ensureServiceAndLoad(target: URL? = nil) {
        if let target = target {
            pendingURL = target
            let fragment = target.fragment ?? "today"
            expectedVerificationView = URLComponents(string: "https://local/?" + fragment)?.queryItems?.first(where: { $0.name == "view" })?.value ?? fragment
        }
        guard !startingService else { return }
        startingService = true
        let python = Bundle.main.object(forInfoDictionaryKey: "GTDPythonPath") as? String ?? "/opt/homebrew/bin/python3"
        let script = Bundle.main.object(forInfoDictionaryKey: "GTDServiceScript") as? String ?? ""
        DispatchQueue.global(qos: .userInitiated).async {
            let process = Process(); process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [script, "--state", self.root.path, "ensure"]
            process.standardOutput = FileHandle.nullDevice; process.standardError = FileHandle.nullDevice
            var success = false
            do { try process.run(); process.waitUntilExit(); success = process.terminationStatus == 0 } catch {}
            DispatchQueue.main.async {
                self.startingService = false
                if success {
                    if let url = self.pendingURL ?? (self.webView?.url == nil ? self.workbenchURL() : nil) {
                        self.pendingURL = nil
                        self.webView?.load(URLRequest(url: url))
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { self.verifyIfRequested(attempt: 0) }
                    }
                }
                else {
                    let alert = NSAlert(); alert.messageText = "工作台暂时无法连接"
                    alert.informativeText = "本机数据仍保留。可以重试连接，或回到 Codex 检查 GTD 状态。"
                    alert.addButton(withTitle: "重试连接"); alert.addButton(withTitle: "稍后")
                    if alert.runModal() == .alertFirstButtonReturn { self.ensureServiceAndLoad() }
                }
            }
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow(); return true }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let url = urls.first, url.scheme == "personalgtd" else { return }
        if verifyDirectory != nil { verificationDone = false }
        showWindow(url: workbenchURL(view: url.host ?? "today"))
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        let origin = message.frameInfo.securityOrigin
        guard message.name == "gtdClipboard", message.frameInfo.isMainFrame,
              origin.protocol == "http", origin.host == "127.0.0.1", origin.port == config()?["port"] as? Int,
              let text = message.body as? String else { return }
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = action.request.url else { decisionHandler(.cancel); return }
        if isWorkbench(url) || url.absoluteString == "about:blank" { decisionHandler(.allow); return }
        decisionHandler(.cancel)
        if action.navigationType == .linkActivated, ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url, action.navigationType == .linkActivated,
           ["https", "http"].contains(url.scheme ?? "") { NSWorkspace.shared.open(url) }
        return nil
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) { recoveringNavigation = false; verifyIfRequested(attempt: 0) }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        let failure = error as NSError
        guard failure.domain == NSURLErrorDomain, failure.code != NSURLErrorCancelled else { return }
        if !recoveringNavigation {
            recoveringNavigation = true
            ensureServiceAndLoad(target: webView.url ?? workbenchURL())
        } else {
            let alert = NSAlert(); alert.messageText = "工作台连接尚未恢复"
            alert.informativeText = "可以稍后从桌面重新打开，或在 Codex 中检查 GTD 状态。"
            alert.addButton(withTitle: "知道了"); alert.runModal()
        }
    }

    func verifyIfRequested(attempt: Int) {
        guard let directory = verifyDirectory, !verificationDone else { return }
        let script = "({heading:document.querySelector('h1')?.textContent||'',view:location.hash.slice(1),navCount:document.querySelectorAll('nav [data-view]').length,sessionReady:!location.hash.includes('access='),nativeClipboard:!!window.webkit?.messageHandlers?.gtdClipboard})"
        webView?.evaluateJavaScript(script) { result, error in
            guard let value = result as? [String: Any], let heading = value["heading"] as? String,
                  ["今天，先做哪一件？", "先理清楚，再放进清单。", "目标要有落脚的下一步。", "停一下，让事情重新有序。"].contains(heading), value["sessionReady"] as? Bool == true else {
                if attempt < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.verifyIfRequested(attempt: attempt + 1) } }
                return
            }
            guard value["view"] as? String == self.expectedVerificationView else {
                if attempt < 40 { DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.verifyIfRequested(attempt: attempt + 1) } }
                return
            }
            self.verificationDone = true
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
                try? data.write(to: directory.appendingPathComponent("webview-app.json"), options: .atomic)
            }
            self.webView?.takeSnapshot(with: nil) { image, error in
                if let data = image?.tiffRepresentation, let bitmap = NSBitmapImageRep(data: data),
                   let png = bitmap.representation(using: .png, properties: [:]) {
                    try? png.write(to: directory.appendingPathComponent("webview-app.png"), options: .atomic)
                }
            }
        }
    }

    func drain() {
        let folder = root.appendingPathComponent("notifications", isDirectory: true)
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        guard !checkingAuthorization, files.contains(where: { $0.lastPathComponent.hasSuffix(".request.json") }) else { return }
        checkingAuthorization = true
        center.getNotificationSettings { settings in
            if settings.authorizationStatus == .notDetermined {
                self.center.requestAuthorization(options: [.alert, .sound]) { granted, error in
                    DispatchQueue.main.async {
                        self.checkingAuthorization = false
                        self.deliverRequests(authorized: granted)
                    }
                }
            } else {
                DispatchQueue.main.async {
                    self.checkingAuthorization = false
                    self.deliverRequests(authorized: settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional)
                }
            }
        }
    }

    func deliverRequests(authorized: Bool) {
        let folder = root.appendingPathComponent("notifications", isDirectory: true)
        let files = (try? FileManager.default.contentsOfDirectory(at: folder, includingPropertiesForKeys: nil)) ?? []
        for file in files where file.lastPathComponent.hasSuffix(".request.json") {
            guard !busy.contains(file.path),
                  let raw = try? Data(contentsOf: file),
                  let value = try? JSONSerialization.jsonObject(with: raw) as? [String: String],
                  let id = value["id"], let title = value["title"], let body = value["body"], let url = value["url"] else { continue }
            let claimed = file.deletingLastPathComponent().appendingPathComponent(id + ".sending.json")
            // Claim by rename: the sender can cancel an unclaimed request before fallback.
            do { try FileManager.default.moveItem(at: file, to: claimed) } catch { continue }
            busy.insert(claimed.path)
            if !authorized {
                finish(claimed, id: id, error: "请在系统设置中允许「Getting Things Done」通知")
                continue
            }
            let content = UNMutableNotificationContent()
            content.title = title
            content.subtitle = "待你确认 · 点击打开工作台"
            content.body = body
            content.userInfo = ["open_url": url]
            center.add(UNNotificationRequest(identifier: id, content: content, trigger: nil)) { error in
                DispatchQueue.main.async { self.finish(claimed, id: id, error: error?.localizedDescription) }
            }
        }
    }

    func finish(_ file: URL, id: String, error: String?) {
        let receipt = file.deletingLastPathComponent().appendingPathComponent(id + ".receipt.json")
        let result: [String: Any] = ["ok": error == nil, "error": error ?? ""]
        if let data = try? JSONSerialization.data(withJSONObject: result) {
            try? data.write(to: receipt, options: .atomic)
            _ = chmod(receipt.path, 0o600)
        }
        try? FileManager.default.removeItem(at: file)
        busy.remove(file.path)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        if response.actionIdentifier != UNNotificationDismissActionIdentifier,
           let value = response.notification.request.content.userInfo["open_url"] as? String,
           let url = URL(string: value), url.scheme == "http", url.host == "127.0.0.1" {
            DispatchQueue.main.async { self.showWindow(url: url) }
        }
        completionHandler()
    }
}

func makeIconset(_ directory: URL) throws {
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    for size in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let pixels = size * scale
            let image = NSImage(size: NSSize(width: pixels, height: pixels), flipped: false) { rect in
                let transform = NSAffineTransform(); transform.scale(by: CGFloat(pixels) / 48); transform.concat()
                NSColor(calibratedRed: 0.137, green: 0.31, blue: 0.255, alpha: 1).setFill()
                NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: 48, height: 48), xRadius: 13, yRadius: 13).fill()
                NSColor(calibratedRed: 0.953, green: 0.949, blue: 0.914, alpha: 1).setStroke()
                let lines = NSBezierPath(); lines.lineWidth = 2.5; lines.lineCapStyle = .round
                for (x, y, end) in [(14.0, 31.0, 34.0), (14.0, 24.0, 23.0), (14.0, 17.0, 23.0)] {
                    lines.move(to: NSPoint(x: x, y: y)); lines.line(to: NSPoint(x: end, y: y))
                }
                lines.stroke()
                NSColor(calibratedRed: 0.816, green: 0.894, blue: 0.573, alpha: 1).setStroke()
                let check = NSBezierPath(); check.lineWidth = 2.8; check.lineCapStyle = .round; check.lineJoinStyle = .round
                check.move(to: NSPoint(x: 27, y: 19)); check.line(to: NSPoint(x: 31, y: 15)); check.line(to: NSPoint(x: 38, y: 24)); check.stroke()
                return true
            }
            if let tiff = image.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff), let png = bitmap.representation(using: .png, properties: [:]) {
                let name = "icon_\(size)x\(size)\(scale == 2 ? "@2x" : "").png"
                try png.write(to: directory.appendingPathComponent(name))
            }
        }
    }
}

umask(0o077)
if let index = CommandLine.arguments.firstIndex(of: "--make-iconset"), index + 1 < CommandLine.arguments.count {
    do { try makeIconset(URL(fileURLWithPath: CommandLine.arguments[index + 1])); exit(0) } catch { exit(2) }
}
let defaultRoot = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".codex/personal-gtd")
let args = CommandLine.arguments
let configuredRoot = Bundle.main.object(forInfoDictionaryKey: "GTDStateRoot") as? String
let root: URL
if let index = args.firstIndex(of: "--state"), index + 1 < args.count { root = URL(fileURLWithPath: args[index + 1]) }
else { root = configuredRoot.map { URL(fileURLWithPath: $0) } ?? defaultRoot }
let delegate = Notifications(root: root)
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
app.delegate = delegate
app.run()
