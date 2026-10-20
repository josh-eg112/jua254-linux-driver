import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Omarchy bar counterpart of the macOS JUA254 Display menu-bar app: status per display,
// Start/Stop, per-display resolution and portrait rotation, start at login, and the log.
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

  function refresh() {
    if (statusProcess.running) return
    statusProcess.command = ["bash", "-c", adapterCommand + " --json"]
    statusProcess.running = true
  }

  function act(command) {
    if (actionProcess.running) return
    lastError = ""
    actionProcess.command = ["bash", "-c", command]
    actionProcess.running = true
  }

  function startStop() { act("systemctl --user " + (running ? "stop " : "start ") + service) }
  function toggleLogin() { act("systemctl --user " + (info.enabled ? "disable " : "enable ") + service) }
  function setMode(a, mode) { if (mode !== a.mode) act(adapterCommand + " " + bar.shellQuote(a.location) + " --mode " + bar.shellQuote(mode)) }
  function setRotation(a, rotate) { if (rotate !== a.rotate) act(adapterCommand + " " + bar.shellQuote(a.location) + " --rotate " + bar.shellQuote(rotate)) }
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
    contentWidth: panel.fittedContentWidth(Style.space(360))
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

          Repeater {
            model: root.adapters

            Column {
              id: adapterColumn
              required property var modelData
              width: column.width
              spacing: Style.space(8)

              PanelSeparator { foreground: root.foreground }

              PanelSectionHeader {
                text: root.adapterTitle(adapterColumn.modelData)
                foreground: root.foreground
                fontFamily: root.fontFamily
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
