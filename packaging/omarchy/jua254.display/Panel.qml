import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs.Commons
import qs.Ui

// Omarchy bar counterpart of the macOS JUA254 Display menu-bar app: status per display,
// Start/Stop, per-display resolution, portrait rotation and position, Identify (a red outline on that
// screen), a drag-to-arrange map of the screens, start at login, and the log.
// All state comes from `jua254-monitor-adapter --json`; changes go through the same tool,
// which the running jua254-monitor service picks up within a second.
Panel {
  id: root
  moduleName: "jua254.display"
  ipcTarget: "jua254.display"

  readonly property string service: "jua254-monitor.service"
  readonly property string adapterCommand: String(setting("adapterCommand", "jua254-monitor-adapter"))
  readonly property int refreshMs: Math.max(5, Number(setting("refreshIntervalSec", 15))) * 1000

  property var info: ({ service: "unknown", enabled: false, modes: [], adapters: [] })
  property string lastError: ""
  property bool busy: actionProcess.running
  property var identified: null  // { output, title } of the screen outlined by Identify
  property bool arranging: false  // a screen is being dragged on the map; refreshing would drop it

  readonly property var layout: info.layout || []

  readonly property var adapters: info.adapters || []
  readonly property bool running: info.service === "active" || info.service === "activating"
  readonly property bool anyError: adapters.some(function(a) { return a.state === "error" })

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgent: bar ? bar.urgent : Color.urgent
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  function summary() {
    if (lastError !== "") return lastError
    if (adapters.length === 0) return "Plug in the adapter first"
    if (!running) return adapters.length === 1 ? "Stopped" : adapters.length + " adapters, stopped"
    var live = adapters.filter(function(a) { return a.state === "streaming" || a.state === "idle" }).length
    if (anyError) return "A display has a problem"
    return live === 1 ? "1 display on" : live + " displays on"
  }

  function adapterStatus(a) {
    if (!running) return "Stopped"
    if (a.state === "streaming") return "Streaming" + (a.fps > 0 ? ", " + a.fps + " fps" : "")
    if (a.state === "idle") return "On, screen unchanged"
    if (a.state === "error") return "USB or encoder errors, try replugging"
    return "Starting"
  }

  function adapterTitle(a) {
    var title = "DISPLAY " + a.number
    if (a.workspace) title += " · WORKSPACE " + a.workspace
    return title
  }

  function modeLabel(mode) {
    var m = String(mode).match(/^(\d+)x(\d+)@(\d+)$/)
    var label = m ? m[1] + " × " + m[2] + ", " + m[3] + " Hz" : String(mode)
    return mode === "1920x1080@60" ? label + " (default)" : label
  }

  function modeOptions(a) {
    var modes = (info.modes || []).slice()
    if (modes.indexOf(a.mode) < 0) modes.push(a.mode)
    return modes.map(function(m) { return { value: m, label: modeLabel(m) } })
  }

  // "Right of Laptop screen" etc. for every screen other than this adapter's own, after "Automatic".
  function positionOptions(a) {
    var sides = { left: "Left of", right: "Right of", above: "Above", below: "Below" }
    var options = [{ value: "auto", label: "Automatic" }]
    var screens = info.screens || [], sideNames = info.sides || []
    screens.forEach(function(screen) {
      if (screen.id === a.location) return
      sideNames.forEach(function(side) {
        options.push({ value: side + ":" + screen.id, label: (sides[side] || side) + " " + screen.label })
      })
    })
    if (!options.some(function(o) { return o.value === a.position }))
      options.push({ value: a.position, label: /^-?\d+x-?\d+$/.test(a.position) ? "As arranged" : a.position })
    return options
  }

  function refresh() {
    if (statusProcess.running || arranging) return
    statusProcess.command = ["bash", "-c", adapterCommand + " --json"]
    statusProcess.running = true
  }

  // Not every Omarchy bar API has shellQuote, so quote here.
  function shellQuote(s) { return "'" + String(s).replace(/'/g, "'\\''") + "'" }

  function act(command) {
    if (actionProcess.running) return
    lastError = ""
    actionProcess.command = ["bash", "-c", command]
    actionProcess.running = true
  }

  function startStop() { act("systemctl --user " + (running ? "stop " : "start ") + service) }
  function toggleLogin() { act("systemctl --user " + (info.enabled ? "disable " : "enable ") + service) }
  function setMode(a, mode) { if (mode !== a.mode) act(adapterCommand + " " + shellQuote(a.location) + " --mode " + shellQuote(mode)) }
  function setPosition(a, position) { if (position !== a.position) act(adapterCommand + " " + shellQuote(a.location) + " --position " + shellQuote(position)) }
  function setRotation(a, rotate) { if (rotate !== a.rotate) act(adapterCommand + " " + shellQuote(a.location) + " --rotate " + shellQuote(rotate)) }
  function identify(output, title) {
    if (!output) return
    identified = { output: output, title: title }
    identifyTimer.restart()
  }

  // Arrange map. Every box is in layout pixels. `snapPosition` puts a dragged box against the nearest
  // edge of another screen, sharing at least a quarter of that edge so the pointer can cross, lined up
  // with the other screen's edge when it is close; null when no spot nearby is free.
  function layoutBounds() {
    if (layout.length === 0) return { x: 0, y: 0, w: 1, h: 1 }
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity
    layout.forEach(function(s) {
      x0 = Math.min(x0, s.x); y0 = Math.min(y0, s.y)
      x1 = Math.max(x1, s.x + s.w); y1 = Math.max(y1, s.y + s.h)
    })
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }
  }

  function snapPosition(name, x, y, w, h) {
    var others = layout.filter(function(s) { return s.name !== name })
    var snap = Math.max(w, h) * 0.1
    function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi) }
    function near(v, edges) {
      for (var i = 0; i < edges.length; i++) if (Math.abs(v - edges[i]) <= snap) return edges[i]
      return v
    }
    function free(cx, cy) {
      return !others.some(function(o) { return cx < o.x + o.w && cx + w > o.x && cy < o.y + o.h && cy + h > o.y })
    }
    var best = null, bestDistance = Infinity
    others.forEach(function(o) {
      var shareY = Math.min(h, o.h) / 4, shareX = Math.min(w, o.w) / 4
      var alongY = Math.round(near(clamp(y, o.y - h + shareY, o.y + o.h - shareY), [o.y, o.y + o.h - h]))
      var alongX = Math.round(near(clamp(x, o.x - w + shareX, o.x + o.w - shareX), [o.x, o.x + o.w - w]))
      var spots = [[o.x - w, alongY], [o.x + o.w, alongY], [alongX, o.y - h], [alongX, o.y + o.h]]
      spots.forEach(function(c) {
        var d = Math.hypot(c[0] - x, c[1] - y)
        if (d < bestDistance && free(c[0], c[1])) { best = c; bestDistance = d }
      })
    })
    return best
  }

  // Save every screen at its place on the map, the dropped one at `spot`, in one go, so none is left to
  // Hyprland's automatic layout to shuffle. The whole layout is shifted so its top-left corner is 0x0.
  function arrange(screen, spot) {
    var places = layout.map(function(s) { return s.name === screen.name ? spot : [s.x, s.y] })
    var minX = Math.min.apply(null, places.map(function(p) { return p[0] }))
    var minY = Math.min.apply(null, places.map(function(p) { return p[1] }))
    var items = layout.map(function(s, i) {
      return shellQuote((s.adapter || s.name) + "=" + (places[i][0] - minX) + "x" + (places[i][1] - minY))
    })
    if (items.length > 0) act(adapterCommand + " --arrange " + items.join(" ") + " >/dev/null")
  }

  function screenTitle(screen) {
    if (!screen.adapter) return screen.label === "Laptop" ? "Laptop screen" : screen.label
    var a = adapters.filter(function(a) { return a.location === screen.adapter })[0]
    return a ? "Display " + a.number : screen.label
  }

  function showLog() {
    close()
    if (bar) bar.run("omarchy-launch-or-focus-tui journalctl --user -f -u " + service)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: refresh()
  onOpenedChanged: if (opened) {
    refresh()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  Timer {
    interval: root.opened ? 3000 : root.refreshMs
    running: true
    repeat: true
    onTriggered: root.refresh()
  }

  Timer { id: identifyTimer; interval: 3000; onTriggered: root.identified = null }

  // Identify: a red outline and the display's name on its screen, click-through, for a few seconds.
  Variants {
    model: root.identified ? Quickshell.screens.filter(function(s) { return s.name === root.identified.output }) : []

    delegate: Component {
      PanelWindow {
        required property var modelData

        screen: modelData
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        mask: Region {}

        WlrLayershell.namespace: "jua254-identify"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None

        anchors { top: true; bottom: true; left: true; right: true }

        Rectangle {
          anchors.fill: parent
          color: "transparent"
          border.color: "#ff2020"
          border.width: 12
        }

        Text {
          anchors.centerIn: parent
          text: root.identified ? root.identified.title : ""
          color: "#ff2020"
          style: Text.Outline
          styleColor: "black"
          font.family: root.fontFamily
          font.pixelSize: 96
          font.bold: true
        }
      }
    }
  }

  // The service applies a change within about a second; read back after it has.
  Timer { id: settleTimer; interval: 1500; onTriggered: root.refresh() }

  Process {
    id: statusProcess
    running: false
    command: []
    stdout: StdioCollector { id: statusStdout; waitForEnd: true }
    stderr: StdioCollector { id: statusStderr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) {
        root.lastError = String(statusStderr.text || "").trim().split("\n").pop() || "jua254-monitor-adapter failed"
        return
      }
      try {
        root.info = JSON.parse(String(statusStdout.text || "{}"))
        root.lastError = ""
      } catch (e) {
        root.lastError = "Unreadable status from jua254-monitor-adapter"
      }
    }
  }

  Process {
    id: actionProcess
    running: false
    command: []
    stderr: StdioCollector { id: actionStderr; waitForEnd: true }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.lastError = String(actionStderr.text || "").trim().split("\n").pop() || "Command failed"
      root.refresh()
      settleTimer.restart()
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.adapters.length > 1 ? "󰍺" : "󰍹"
    opacity: root.running && root.adapters.length > 0 ? 1.0 : 0.5
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.startStop()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight, Style.space(620))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        if (t === "s" || t === "S") root.startStop()
        else if (t === "r" || t === "R") root.refresh()
        else if (t === "l" || t === "L") root.showLog()
      }

      Flickable {
        id: panelFlick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: panelFlick.width
          spacing: Style.space(12)

          PanelHero {
            id: hero
            width: parent.width
            title: "JUA254 Display"
            meta: root.summary()
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconOpacity: root.running ? 1.0 : 0.5
            iconComponent: Component {
              Text {
                text: root.adapters.length > 1 ? "󰍺" : "󰍹"
                color: root.anyError || root.lastError !== "" ? root.urgent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              ToggleSwitch {
                id: powerSwitch
                visible: root.adapters.length > 0
                checked: root.running
                busy: root.busy
                foreground: hero.foreground
                onToggled: root.startStop()

                PanelToolTip {
                  visible: powerSwitch.containsMouse
                  text: root.running ? "Stop" : "Start"
                  fontFamily: hero.fontFamily
                }
              }
            }
          }

          Column {
            width: column.width
            spacing: Style.space(8)
            visible: root.layout.length > 1

            PanelSeparator { foreground: root.foreground }

            PanelSectionHeader {
              width: parent.width
              text: "ARRANGE"
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              textFormat: Text.PlainText
              width: parent.width
              text: "Drag the screens to match your desk. Click one to see which it is."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
              wrapMode: Text.WordWrap
            }

            Item {
              id: arrangeMap
              width: parent.width
              height: Style.space(180)
              clip: true

              readonly property var bounds: root.layoutBounds()
              // Room around the screens to drop one above, below or beside them all.
              readonly property real pad: Math.max.apply(null, root.layout.map(function(s) { return Math.max(s.w, s.h) }).concat([1])) * 0.6
              readonly property real scale: Math.min(width / (bounds.w + 2 * pad), height / (bounds.h + 2 * pad))
              readonly property real originX: (width - bounds.w * scale) / 2 - bounds.x * scale
              readonly property real originY: (height - bounds.h * scale) / 2 - bounds.y * scale

              Rectangle {
                anchors.fill: parent
                color: "transparent"
                border.color: root.dim
                border.width: 1
                radius: 4
              }

              Repeater {
                model: root.layout

                Rectangle {
                  id: box
                  required property var modelData
                  readonly property bool adapterScreen: !!modelData.adapter
                  readonly property real homeX: arrangeMap.originX + modelData.x * arrangeMap.scale
                  readonly property real homeY: arrangeMap.originY + modelData.y * arrangeMap.scale

                  x: homeX
                  y: homeY
                  z: dragArea.pressed ? 2 : 1
                  width: modelData.w * arrangeMap.scale
                  height: modelData.h * arrangeMap.scale
                  radius: 3
                  color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, adapterScreen ? 0.22 : 0.12)
                  border.color: dragArea.pressed ? root.urgent : root.foreground
                  border.width: dragArea.pressed ? 2 : 1

                  Text {
                    anchors.centerIn: parent
                    width: parent.width - 4
                    horizontalAlignment: Text.AlignHCenter
                    textFormat: Text.PlainText
                    text: box.modelData.label
                    elide: Text.ElideRight
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Math.max(8, Math.min(box.height * 0.45, box.adapterScreen ? Style.font.body : Style.font.bodySmall))
                    font.bold: box.adapterScreen
                  }

                  MouseArea {
                    id: dragArea
                    anchors.fill: parent
                    cursorShape: pressed ? Qt.ClosedHandCursor : Qt.OpenHandCursor
                    drag.target: box
                    drag.threshold: 2

                    onPressed: {
                      root.arranging = true
                      root.identify(box.modelData.name, root.screenTitle(box.modelData))
                    }
                    onCanceled: { root.arranging = false; box.x = Qt.binding(function() { return box.homeX }); box.y = Qt.binding(function() { return box.homeY }) }
                    onReleased: {
                      root.arranging = false
                      if (Math.abs(box.x - box.homeX) < 1 && Math.abs(box.y - box.homeY) < 1) return
                      var s = box.modelData
                      var spot = root.snapPosition(s.name, Math.round((box.x - arrangeMap.originX) / arrangeMap.scale),
                                                   Math.round((box.y - arrangeMap.originY) / arrangeMap.scale), s.w, s.h)
                      if (spot && (spot[0] !== s.x || spot[1] !== s.y)) {
                        box.x = arrangeMap.originX + spot[0] * arrangeMap.scale
                        box.y = arrangeMap.originY + spot[1] * arrangeMap.scale
                        root.arrange(s, spot)
                      } else {
                        box.x = Qt.binding(function() { return box.homeX })
                        box.y = Qt.binding(function() { return box.homeY })
                      }
                    }
                  }
                }
              }
            }
          }

          Repeater {
            model: root.adapters

            Column {
              id: adapterColumn
              required property var modelData
              width: column.width
              spacing: Style.space(8)

              PanelSeparator { foreground: root.foreground }

              RowLayout {
                width: parent.width
                spacing: Style.space(8)

                PanelSectionHeader {
                  Layout.fillWidth: true
                  text: root.adapterTitle(adapterColumn.modelData)
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                }

                Button {
                  text: "Identify"
                  tooltipText: "Outline this display's screen in red"
                  bordered: true
                  enabled: !!adapterColumn.modelData.output
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  onClicked: root.identify(adapterColumn.modelData.output, "Display " + adapterColumn.modelData.number)
                }
              }

              Text {
                textFormat: Text.PlainText
                width: parent.width
                text: root.adapterStatus(adapterColumn.modelData)
                color: adapterColumn.modelData.state === "error" ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                wrapMode: Text.WordWrap
              }

              Dropdown {
                width: parent.width
                label: "Resolution"
                value: adapterColumn.modelData.mode
                options: root.modeOptions(adapterColumn.modelData)
                foreground: root.foreground
                fontFamily: root.fontFamily
                onChanged: function(value) { root.setMode(adapterColumn.modelData, value) }
              }

              ButtonGroup {
                value: adapterColumn.modelData.rotate
                focusable: false
                foreground: root.foreground
                fontFamily: root.fontFamily
                options: [
                  { value: "none", label: "Landscape" },
                  { value: "left", label: "Portrait left" },
                  { value: "right", label: "Portrait right" }
                ]
                onChanged: function(value) { root.setRotation(adapterColumn.modelData, value) }
              }

              Dropdown {
                width: parent.width
                label: "Position"
                value: adapterColumn.modelData.position || "auto"
                options: root.positionOptions(adapterColumn.modelData)
                foreground: root.foreground
                fontFamily: root.fontFamily
                onChanged: function(value) { root.setPosition(adapterColumn.modelData, value) }
              }
            }
          }

          PanelSeparator { foreground: root.foreground }

          Toggle {
            width: parent.width
            label: "Start at login"
            description: "Run the driver whenever you log in"
            checked: root.info.enabled === true
            foreground: root.foreground
            fontFamily: root.fontFamily
            onClicked: root.toggleLogin()
          }

          RowLayout {
            width: parent.width
            spacing: Style.space(8)

            Text {
              textFormat: Text.PlainText
              Layout.fillWidth: true
              text: "Show log"
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: root.showLog() }
            }

            PanelActionButton {
              iconText: "󰦪"
              tooltipText: "Open the driver log in a terminal"
              foreground: root.foreground
              fontFamily: root.fontFamily
              onClicked: root.showLog()
            }
          }
        }
      }
    }
  }
}
