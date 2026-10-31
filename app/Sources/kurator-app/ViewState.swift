import SwiftUI

/// Замена `@State`: в SDK 27 он объявлен макросом, а плагин макросов
/// поставляется только с полным Xcode — в Command Line Tools его нет,
/// и сборка через `swift build` падает. Структура `State<Value>` в SDK
/// осталась, поэтому оборачиваем её своим property wrapper'ом с тем же
/// поведением. Когда проект переедет на Xcode, это можно будет заменить
/// обратно на `@State` точечно.
@propertyWrapper
struct ViewState<Value>: DynamicProperty {
    private var state: State<Value>

    init(wrappedValue: Value) {
        state = State(initialValue: wrappedValue)
    }

    var wrappedValue: Value {
        get { state.wrappedValue }
        nonmutating set { state.wrappedValue = newValue }
    }

    var projectedValue: Binding<Value> { state.projectedValue }
}
