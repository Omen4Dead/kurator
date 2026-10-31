// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "kurator-app",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "kurator-app",
            path: "Sources/kurator-app"
        )
    ]
)
