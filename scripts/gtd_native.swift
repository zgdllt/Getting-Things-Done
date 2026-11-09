import Foundation
import EventKit
import CoreServices
import Darwin

setbuf(stdout, nil)

struct Failure: Error { let message: String }
func fail(_ message: String) throws -> Never { throw Failure(message: message) }
func output(_ value: Any) {
    do {
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        print(String(data: data, encoding: .utf8)!)
    } catch { print("{\"ok\":false,\"error\":\"JSON encoding failed\"}") }
}
func status(_ kind: EKEntityType) -> String {
    switch EKEventStore.authorizationStatus(for: kind) {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "fullAccess"
    case .writeOnly: return "writeOnly"
    @unknown default: return "unknown"
    }
}
func text(_ object: [String: Any], _ key: String) throws -> String {
    guard let value = object[key] as? String, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
        try fail("Missing nonempty field: \(key)")
    }
    return value
}
let iso = ISO8601DateFormatter()
let fractionalISO = ISO8601DateFormatter()
fractionalISO.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
func date(_ value: String) throws -> Date {
    let pattern = #"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:?[0-9]{2})$"#
    let regex = try NSRegularExpression(pattern: pattern)
    if let match = regex.firstMatch(in: value, range: NSRange(value.startIndex..., in: value)) {
        func part(_ index: Int) -> String {
            guard let range = Range(match.range(at: index), in: value) else { return "" }
            return String(value[range])
        }
        let zone = part(8)
        let digits = zone.filter { $0.isNumber }
        var offset = 0
        if zone != "Z" {
            let hours = Int(digits.prefix(2))!, minutes = Int(digits.suffix(2))!
            guard hours <= 23, minutes <= 59 else { try fail("Invalid timezone offset") }
            offset = (hours * 3600 + minutes * 60) * (zone.first == "-" ? -1 : 1)
        }
        guard let timezone = TimeZone(secondsFromGMT: offset) else { try fail("Invalid timezone offset") }
        var calendar = Calendar(identifier: .gregorian); calendar.timeZone = timezone
        let parts = DateComponents(timeZone: timezone, year: Int(part(1)), month: Int(part(2)),
            day: Int(part(3)), hour: Int(part(4)), minute: Int(part(5)), second: Int(part(6)))
        guard (parts.year ?? 0) > 0, (parts.hour ?? 99) < 24, (parts.minute ?? 99) < 60,
              (parts.second ?? 99) < 60, parts.isValidDate(in: calendar),
              let base = calendar.date(from: parts) else { try fail("Invalid calendar date/time") }
        return base.addingTimeInterval(part(7).isEmpty ? 0 : Double("0." + part(7))!)
    }
    let format = DateFormatter()
    format.locale = Locale(identifier: "en_US_POSIX")
    format.timeZone = TimeZone.current
    format.dateFormat = "yyyy-MM-dd"
    format.isLenient = false
    if value.count == 10, let parsed = format.date(from: value), format.string(from: parsed) == value { return parsed }
    try fail("Invalid ISO date/time: \(value)")
}
func dueComponents(_ value: String) throws -> DateComponents {
    let parsed = try date(value)
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = TimeZone.current
    let fields: Set<Calendar.Component> = value.count == 10 ? [.year,.month,.day] : [.year,.month,.day,.hour,.minute,.second]
    var result = calendar.dateComponents(fields, from: parsed)
    result.calendar = calendar
    if value.count != 10 { result.timeZone = calendar.timeZone }
    return result
}
func dueValue(_ value: DateComponents?) -> Any {
    guard let value = value, let y = value.year, let m = value.month, let d = value.day else { return NSNull() }
    if value.hour == nil {
        return ["value": String(format: "%04d-%02d-%02d", y,m,d), "all_day": true]
    }
    var calendar = value.calendar ?? Calendar(identifier: .gregorian)
    calendar.timeZone = value.timeZone ?? TimeZone.current
    guard let parsed = calendar.date(from: value) else { return NSNull() }
    return ["value": iso.string(from: parsed), "all_day": false]
}
func stamp(_ date: Date?) -> Any { date.map { fractionalISO.string(from: $0) } ?? NSNull() }
final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var storage: T?
    func put(_ value: T) { lock.lock(); storage = value; lock.unlock() }
    func get() -> T? { lock.lock(); defer { lock.unlock() }; return storage }
}
func wait<T>(_ box: Box<T>, timeout: TimeInterval = 120) throws -> T {
    let until = Date().addingTimeInterval(timeout)
    while Date() < until {
        if let value = box.get() { return value }
        RunLoop.current.run(until: Date().addingTimeInterval(0.05))
    }
    try fail("Operation has not completed; permission or service may still be pending")
}
func require(_ kind: EKEntityType) throws {
    guard status(kind) == "fullAccess" else { try fail("permission_required:\(kind == .event ? "calendar" : "reminders"):\(status(kind))") }
}
func reminders(_ store: EKEventStore, predicate: NSPredicate) throws -> [EKReminder] {
    let box = Box<[EKReminder]>()
    store.fetchReminders(matching: predicate) { box.put($0 ?? []) }
    return try wait(box, timeout: 60)
}
func listValue(_ list: EKCalendar) -> [String: Any] {
    ["id": list.calendarIdentifier, "title": list.title ?? "", "source": list.source.title ?? "",
     "source_id": list.source.sourceIdentifier, "writable": list.allowsContentModifications]
}
func reminderValue(_ item: EKReminder) -> [String: Any] {
    ["id": item.calendarItemIdentifier, "list_id": item.calendar.calendarIdentifier,
     "list": item.calendar.title ?? "", "title": item.title ?? "", "notes": item.notes ?? "",
     "url": item.url?.absoluteString ?? "", "completed": item.isCompleted,
     "due": dueValue(item.dueDateComponents), "priority": item.priority,
     "created_at": stamp(item.creationDate), "modified_at": stamp(item.lastModifiedDate),
     "completed_at": stamp(item.completionDate)]
}
func eventValue(_ item: EKEvent) -> [String: Any] {
    ["id": item.calendarItemIdentifier, "event_id": item.eventIdentifier ?? "",
     "calendar_id": item.calendar.calendarIdentifier, "calendar": item.calendar.title ?? "",
     "title": item.title ?? "", "start": iso.string(from: item.startDate),
     "end": iso.string(from: item.endDate), "all_day": item.isAllDay,
     "original_start": fractionalISO.string(from: item.startDate),
     "occurrence_date": stamp(item.occurrenceDate), "detached": item.isDetached,
     "recurring": item.hasRecurrenceRules,
     "notes": item.notes ?? "", "location": item.location ?? "", "url": item.url?.absoluteString ?? "",
     "modified_at": stamp(item.lastModifiedDate)]
}
func input() throws -> [String: Any] {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { try fail("Expected a JSON object") }
    return value
}
func marker(_ id: String) -> String { "[personal-gtd:\(id)]" }
func markedNotes(_ notes: String, _ id: String) -> String { notes + (notes.isEmpty ? "" : "\n\n") + marker(id) }
func replacingNotes(_ old: String?, _ replacement: String) -> String {
    let markers = (old ?? "").components(separatedBy: .newlines)
        .filter { $0.hasPrefix("[personal-gtd:") && $0.hasSuffix("]") }
    var result = replacement
    for entry in markers where !replacement.components(separatedBy: .newlines).contains(entry) {
        result += (result.isEmpty ? "" : "\n\n") + entry
    }
    return result
}
func checkVersion(_ item: EKCalendarItem, _ request: [String: Any]) throws {
    if let expected = request["expected_modified_at"] as? String,
       fractionalISO.string(from: item.lastModifiedDate ?? .distantPast) != expected {
        try fail("conflict: item changed after the supplied snapshot; read it again")
    }
}
func patchReminder(_ item: EKReminder, _ request: [String: Any]) throws {
    if request["title"] != nil { item.title = try text(request, "title") }
    if let notes = request["notes"] as? String { item.notes = replacingNotes(item.notes, notes) }
    if let value = request["priority"] as? Int {
        guard (0...9).contains(value) else { try fail("priority must be 0...9") }
        item.priority = value
    }
    if let value = request["completed"] as? Bool { item.isCompleted = value }
    if let value = request["due"] as? String { item.dueDateComponents = try dueComponents(value) }
    else if request["due"] is NSNull { item.dueDateComponents = nil }
    if let value = request["url"] as? String { item.url = value.isEmpty ? nil : URL(string: value) }
}
func writableList(_ store: EKEventStore, _ id: String, _ kind: EKEntityType) throws -> EKCalendar {
    guard let list = store.calendars(for: kind).first(where: { $0.calendarIdentifier == id }),
          list.allowsContentModifications else { try fail("Unknown or read-only destination list/calendar") }
    return list
}
func apply(_ request: [String: Any], store: EKEventStore) throws -> [String: Any] {
    let action = try text(request, "action"), op = try text(request, "op_id")
    switch action {
    case "create_list":
        try require(.reminder)
        let title = try text(request, "title")
        let sources = store.sources
        let source: EKSource
        if let id = request["source_id"] as? String {
            guard let found = sources.first(where: { $0.sourceIdentifier == id }) else { try fail("Unknown source_id") }
            source = found
        } else {
            guard let found = store.defaultCalendarForNewReminders()?.source else { try fail("No default reminders account") }
            source = found
        }
        let existing = store.calendars(for: .reminder).filter { $0.title == title && $0.source.sourceIdentifier == source.sourceIdentifier }
        guard existing.count <= 1 else { try fail("Ambiguous existing reminder list") }
        if let found = existing.first { return ["ok": true, "reused": true, "item": listValue(found)] }
        let list = EKCalendar(for: .reminder, eventStore: store)
        list.title = title; list.source = source
        try store.saveCalendar(list, commit: true)
        return ["ok": true, "item": listValue(list)]
    case "create_reminder":
        try require(.reminder)
        let list = try writableList(store, text(request, "list_id"), .reminder)
        let existing = try reminders(store, predicate: store.predicateForReminders(in: [list]))
            .filter { ($0.notes ?? "").components(separatedBy: .newlines).contains(marker(op)) }
        guard existing.count <= 1 else { try fail("Duplicate operation marker; inspect matching reminders") }
        if let found = existing.first { return ["ok": true, "reused": true, "item": reminderValue(found)] }
        let item = EKReminder(eventStore: store); item.calendar = list
        try patchReminder(item, request)
        item.title = try text(request, "title")
        item.notes = markedNotes(request["notes"] as? String ?? "", op)
        try store.save(item, commit: true)
        return ["ok": true, "item": reminderValue(item)]
    case "update_reminder":
        try require(.reminder)
        guard let item = store.calendarItem(withIdentifier: try text(request, "id")) as? EKReminder else { try fail("Reminder not found") }
        try checkVersion(item, request)
        if let list = request["list_id"] as? String { item.calendar = try writableList(store, list, .reminder) }
        try patchReminder(item, request)
        try store.save(item, commit: true)
        return ["ok": true, "item": reminderValue(item)]
    case "create_event", "update_event":
        try require(.event)
        let item: EKEvent
        if action == "update_event" {
            let id = try text(request, "id")
            let originalStart = try date(text(request, "original_start"))
            let query = store.predicateForEvents(withStart: originalStart.addingTimeInterval(-1),
                end: originalStart.addingTimeInterval(1), calendars: nil)
            var matches = store.events(matching: query).filter {
                $0.calendarItemIdentifier == id && abs($0.startDate.timeIntervalSince(originalStart)) < 0.001
            }
            if let raw = request["occurrence_date"] as? String {
                let expected = try date(raw)
                matches = matches.filter { $0.occurrenceDate.map { abs($0.timeIntervalSince(expected)) < 0.001 } ?? false }
            }
            guard matches.count == 1, let found = matches.first else {
                try fail("Event occurrence not found or changed; read its current snapshot")
            }
            try checkVersion(found, request); item = found
        } else { item = EKEvent(eventStore: store) }
        if let id = request["calendar_id"] as? String { item.calendar = try writableList(store, id, .event) }
        if action == "create_event" && item.calendar == nil { try fail("calendar_id is required") }
        if request["title"] != nil { item.title = try text(request, "title") }
        if let value = request["start"] as? String { item.startDate = try date(value) }
        if let value = request["end"] as? String { item.endDate = try date(value) }
        guard item.startDate != nil, item.endDate != nil, item.endDate > item.startDate else { try fail("A valid start and later end are required") }
        if let value = request["all_day"] as? Bool { item.isAllDay = value }
        if let value = request["notes"] as? String { item.notes = replacingNotes(item.notes, value) }
        if let value = request["location"] as? String { item.location = value }
        if let value = request["url"] as? String { item.url = value.isEmpty ? nil : URL(string: value) }
        if action == "create_event" {
            item.title = try text(request, "title")
            let predicate = store.predicateForEvents(withStart: item.startDate.addingTimeInterval(-86400),
                end: item.endDate.addingTimeInterval(86400), calendars: [item.calendar])
            let existing = store.events(matching: predicate).filter { ($0.notes ?? "").components(separatedBy: .newlines).contains(marker(op)) }
            guard existing.count <= 1 else { try fail("Duplicate operation marker; inspect matching events") }
            if let found = existing.first { return ["ok": true, "reused": true, "item": eventValue(found)] }
            item.notes = markedNotes(item.notes ?? "", op)
        }
        try store.save(item, span: .thisEvent, commit: true)
        return ["ok": true, "item": eventValue(item)]
    default: try fail("Unsupported action: \(action)")
    }
}
func fileTags(_ url: URL) throws -> [String] {
    try url.resourceValues(forKeys: [.tagNamesKey]).tagNames ?? []
}
let userTagsAttribute = "com.apple.metadata:_kMDItemUserTags"
func rawFileTags(_ url: URL) throws -> [String] {
    let length = getxattr(url.path, userTagsAttribute, nil, 0, 0, XATTR_NOFOLLOW)
    if length < 0 {
        if errno == ENOATTR { return [] }
        try fail("无法读取标签元数据：\(String(cString: strerror(errno)))")
    }
    var data = Data(count: length)
    let count = data.withUnsafeMutableBytes { getxattr(url.path, userTagsAttribute, $0.baseAddress, length, 0, XATTR_NOFOLLOW) }
    guard count == length,
          let tags = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String] else {
        try fail("标签元数据在读取时变化，请重试")
    }
    return tags
}
func rawTagName(_ value: String) -> String {
    guard let separator = value.lastIndex(of: "\n"), Int(value[value.index(after: separator)...]) != nil else { return value }
    return String(value[..<separator])
}
func rawTagColor(_ value: String) -> Int {
    Int(value.components(separatedBy: "\n").last ?? "") ?? 0
}
func physicalFileURL(_ url: URL) -> URL {
    guard let resolved = realpath(url.path, nil) else { return url }
    defer { free(resolved) }
    return URL(fileURLWithPath: String(cString: resolved))
}
func fileRecord(_ source: URL) throws -> [String: Any] {
    let values = try source.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey, .isPackageKey])
    guard values.isSymbolicLink != true else { try fail("不会沿符号链接整理文件：\(source.path)") }
    let url = physicalFileURL(source)
    let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    let identity = "\(attrs[.systemNumber] ?? ""):\(attrs[.systemFileNumber] ?? "")"
    let modified = (attrs[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
    let size = (attrs[.size] as? NSNumber)?.int64Value ?? 0
    let raw = try rawFileTags(url)
    var colors: [String: Int] = [:]
    for tag in raw { colors[rawTagName(tag)] = rawTagColor(tag) }
    return ["path": url.path, "name": url.lastPathComponent, "directory": values.isDirectory == true,
            "package": values.isPackage == true, "identity": identity,
            "signature": "\(identity):\(modified):\(size)", "size": size, "modified_at": modified,
            "tags": try fileTags(url), "tag_colors": colors]
}
func finderTags(_ records: [[String: Any]] = []) -> [[String: Any]] {
    var names = CFPreferencesCopyAppValue("FavoriteTagNames" as CFString, "com.apple.finder" as CFString) as? [String] ?? []
    // Finder's synced tag catalog is optional; selected files supply additional tags.
    let path = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/SyncedPreferences/com.apple.finder.plist")
    var colors: [String: Int] = [:]
    if let data = try? Data(contentsOf: path),
       let plist = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
       let values = plist["values"] as? [String: Any],
       let dictionary = values["FinderTagDict"] as? [String: Any],
       let value = dictionary["value"] as? [String: Any],
       let tags = value["FinderTags"] as? [[String: Any]] {
        for tag in tags {
            if let name = tag["n"] as? String {
                names.append(name); colors[name] = tag["l"] as? Int ?? 0
            }
        }
    }
    for record in records {
        names += record["tags"] as? [String] ?? []
        colors.merge(record["tag_colors"] as? [String: Int] ?? [:]) { _, observed in observed }
    }
    let standard = ["灰色": 1, "绿色": 2, "紫色": 3, "蓝色": 4, "黄色": 5, "红色": 6, "橙色": 7,
                    "Gray": 1, "Green": 2, "Purple": 3, "Blue": 4, "Yellow": 5, "Red": 6, "Orange": 7]
    var seen = Set<String>()
    return names.filter { !$0.isEmpty && seen.insert($0).inserted }.map {
        ["name": $0, "color": colors[$0] ?? standard[$0] ?? 0]
    }
}
func inspectFiles(_ request: [String: Any]) throws -> [String: Any] {
    guard request["recursive"] == nil, request["scopes"] == nil,
          let paths = request["paths"] as? [String] else {
        try fail("旧扫描协议已废弃，files-inspect 只接受明确的 paths")
    }
    let excluded = (request["exclude"] as? [String] ?? []).map { physicalFileURL(URL(fileURLWithPath: $0)).path }
    func excludedPath(_ url: URL) -> Bool { excluded.contains { url.path == $0 || url.path.hasPrefix($0 + "/") } }
    var records: [[String: Any]] = [], errors: [[String: String]] = [], seen = Set<String>()
    func add(_ url: URL) {
        guard !excludedPath(url) else { return }
        guard seen.insert(url.path).inserted else { return }
        do { records.append(try fileRecord(url)) }
        catch { errors.append(["path": url.path, "error": error.localizedDescription]) }
    }
    for path in paths {
        let source = URL(fileURLWithPath: path)
        if (try? source.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
            errors.append(["path": source.path, "error": "不会沿符号链接整理文件"]); continue
        }
        let url = physicalFileURL(source)
        add(url)
        // A selected directory is one organizing object. Never enumerate its descendants.
    }
    return ["ok": true, "items": records, "tags": finderTags(records), "errors": errors]
}
func updateFileTags(_ request: [String: Any]) throws -> [String: Any] {
    guard let entries = request["items"] as? [[String: Any]] else { try fail("Missing file operations") }
    var results: [[String: Any]] = []
    for entry in entries {
        let path = entry["path"] as? String ?? ""
        do {
            let url = URL(fileURLWithPath: path)
            let before = try fileRecord(url)
            guard before["identity"] as? String == entry["identity"] as? String else {
                try fail("文件已被替换，请重新预览")
            }
            let current = before["tags"] as? [String] ?? []
            let expected = entry["before"] as? [String] ?? []
            let added = entry["add"] as? [String] ?? []
            let remove = entry["remove"] as? [String] ?? []
            if request["undo"] as? Bool == true {
                let ownedColors = entry["remove_colors"] as? [String: Int] ?? [:]
                let currentColors = before["tag_colors"] as? [String: Int] ?? [:]
                guard remove.allSatisfy({ !current.contains($0) || currentColors[$0] == ownedColors[$0] }) else {
                    try fail("本次标签的颜色后来发生变化，请在 Finder 中核对后处理")
                }
            }
            var after = current.filter { !remove.contains($0) }
            for name in added where !after.contains(name) { after.append(name) }
            if request["undo"] as? Bool != true {
                guard Set(current) == Set(expected), before["signature"] as? String == entry["signature"] as? String else {
                    try fail("文件或标签在预览后发生变化，请重新预览")
                }
                if let expectedColors = entry["before_colors"] as? [String: Int],
                   (before["tag_colors"] as? [String: Int] ?? [:]) != expectedColors {
                    try fail("标签颜色在预览后发生变化，请重新预览")
                }
            }
            let original = try rawFileTags(url)
            let colors = entry["colors"] as? [String: Int] ?? [:]
            let raw = after.map { name in
                original.first(where: { rawTagName($0) == name }) ?? "\(name)\n\(colors[name] ?? 0)"
            }
            let encoded = try PropertyListSerialization.data(fromPropertyList: raw, format: .binary, options: 0)
            let saved = encoded.withUnsafeBytes { setxattr(url.path, userTagsAttribute, $0.baseAddress, encoded.count, 0, XATTR_NOFOLLOW) }
            guard saved == 0 else { try fail("无法保存标签：\(String(cString: strerror(errno)))") }
            let verified = try fileTags(URL(fileURLWithPath: path))
            guard Set(verified) == Set(after), try rawFileTags(url) == raw else {
                try fail("标签写入后未能核对，请刷新检查")
            }
            results.append(["path": path, "ok": true, "before": current, "after": verified,
                            "tag_colors": Dictionary(uniqueKeysWithValues: raw.map { (rawTagName($0), rawTagColor($0)) })])
        } catch {
            let message = (error as? Failure)?.message ?? error.localizedDescription
            results.append(["path": path, "ok": false, "error": message])
        }
    }
    return ["ok": true, "items": results]
}
func watchFiles(_ request: [String: Any]) throws {
    guard let scopes = request["scopes"] as? [[String: Any]] else { try fail("Missing file scopes") }
    let roots = try scopes.map { physicalFileURL(URL(fileURLWithPath: try text($0, "path"))).path }
    let excluded = (request["exclude"] as? [String] ?? []).map { physicalFileURL(URL(fileURLWithPath: $0)).path }
    // The callback owns only selected roots, not a list of descendant files.
    final class WatchedFiles {
        let roots: [String], excluded: [String]
        init(_ roots: [String], _ excluded: [String]) { self.roots = roots; self.excluded = excluded }
    }
    let context = WatchedFiles(roots, excluded)
    let pointer = Unmanaged.passRetained(context).toOpaque()
    defer { Unmanaged<WatchedFiles>.fromOpaque(pointer).release() }
    var streamContext = FSEventStreamContext(version: 0, info: pointer, retain: nil, release: nil, copyDescription: nil)
    let callback: FSEventStreamCallback = { _, info, count, eventPaths, flags, _ in
        guard let info else { return }
        let context = Unmanaged<WatchedFiles>.fromOpaque(info).takeUnretainedValue()
        let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] ?? []
        var changed = Set<String>(), uncertain = false
        let lost = FSEventStreamEventFlags(kFSEventStreamEventFlagMustScanSubDirs |
            kFSEventStreamEventFlagUserDropped | kFSEventStreamEventFlagKernelDropped |
            kFSEventStreamEventFlagRootChanged | kFSEventStreamEventFlagEventIdsWrapped)
        for (index, path) in paths.enumerated() {
            if context.excluded.contains(where: { path == $0 || path.hasPrefix($0 + "/") }) { continue }
            if index < count, flags[index] & lost != 0 { uncertain = true }
            for root in context.roots where path == root || path.hasPrefix(root + "/") || root.hasPrefix(path + "/") {
                changed.insert(root)
            }
        }
        if !changed.isEmpty {
            output(["kind": "files_changed", "roots": changed.sorted(), "uncertain": uncertain])
        }
    }
    let watched = Set(roots.map { path -> String in
        let url = URL(fileURLWithPath: path)
        return (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
            ? path : url.deletingLastPathComponent().path
    })
    let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents |
        kFSEventStreamCreateFlagWatchRoot | kFSEventStreamCreateFlagUseCFTypes)
    guard let stream = FSEventStreamCreate(nil, callback, &streamContext, Array(watched) as CFArray,
        FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 2.0, flags) else { try fail("Cannot watch selected roots") }
    defer { FSEventStreamStop(stream); FSEventStreamInvalidate(stream); FSEventStreamRelease(stream) }
    FSEventStreamScheduleWithRunLoop(stream, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
    guard FSEventStreamStart(stream) else { try fail("Cannot watch selected roots") }
    output(["ok": true, "kind": "watching"])
    CFRunLoopRun()
}
func watch(_ roots: [String]) throws {
    let callback: FSEventStreamCallback = { _, _, count, _, _, _ in
        if count > 0 { print("{\"kind\":\"history_changed\"}"); fflush(stdout) }
    }
    let flags = FSEventStreamCreateFlags(kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot)
    guard let stream = FSEventStreamCreate(nil, callback, nil, roots as CFArray,
        FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 1.0, flags) else { try fail("Cannot create history watcher") }
    FSEventStreamScheduleWithRunLoop(stream, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
    guard FSEventStreamStart(stream) else { try fail("Cannot start history watcher") }
    output(["ok": true, "kind": "watching"])
    CFRunLoopRun()
    FSEventStreamStop(stream); FSEventStreamInvalidate(stream); FSEventStreamRelease(stream)
}
do {
    let command = CommandLine.arguments.dropFirst().first ?? "status"
    if command == "files-watch" {
        try watchFiles(input())
    } else if command == "watch" {
        guard CommandLine.arguments.count >= 3 else { try fail("watch requires at least one directory") }
        try watch(Array(CommandLine.arguments.dropFirst(2)))
    } else if command == "files-scan" {
        try fail("files-scan 递归扫描接口已废弃，请使用 files-inspect")
    } else if command == "files-inspect" {
        output(try inspectFiles(input()))
    } else if command == "files-tags" {
        output(["ok": true, "tags": finderTags()])
    } else if command == "files-apply" {
        output(try updateFileTags(input()))
    } else if command == "status" {
        output(["ok": true, "calendar": status(.event), "reminders": status(.reminder)])
    } else if command == "authorize" {
        let store = EKEventStore(), box = Box<[String: Any]>()
        store.requestFullAccessToReminders { granted, error in box.put(["granted": granted, "error": error?.localizedDescription ?? ""]) }
        let remindersResult = try wait(box)
        let second = Box<[String: Any]>()
        store.requestFullAccessToEvents { granted, error in second.put(["granted": granted, "error": error?.localizedDescription ?? ""]) }
        let calendarResult = try wait(second)
        output(["ok": true, "reminders": remindersResult, "calendar": calendarResult])
    } else if command == "snapshot" {
        let request = try input(), store = EKEventStore()
        var result: [String: Any] = ["ok": true, "generated_at": iso.string(from: Date()),
            "permissions": ["calendar": status(.event), "reminders": status(.reminder)]]
        if status(.reminder) == "fullAccess" {
            let lists = store.calendars(for: .reminder)
            var items = try reminders(store, predicate: store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: nil, calendars: nil))
            if let since = request["completed_since"] as? String {
                items += try reminders(store, predicate: store.predicateForCompletedReminders(withCompletionDateStarting: try date(since), ending: Date(), calendars: nil))
            }
            result["lists"] = lists.map(listValue); result["reminders"] = items.map(reminderValue)
        }
        if status(.event) == "fullAccess" {
            let start = try date(text(request, "start")), end = try date(text(request, "end"))
            guard end > start else { try fail("end must be later than start") }
            result["calendars"] = store.calendars(for: .event).map(listValue)
            result["events"] = store.events(matching: store.predicateForEvents(withStart: start, end: end, calendars: nil)).map(eventValue)
        }
        output(result)
    } else if command == "apply" { output(try apply(input(), store: EKEventStore())) }
    else { try fail("Unknown command: \(command)") }
} catch let error as Failure { output(["ok": false, "error": error.message]); exit(2) }
catch { output(["ok": false, "error": error.localizedDescription]); exit(2) }
