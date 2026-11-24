// Jarvis B: window behavior in KWin (Plasma 6).
//
// PANEL (popup, title "Jarvis"):
//  - positions on the cursor monitor: horizontally centered and near
//    from the top (only for the first 1.5 s; then you can drag it);
//  - appears on top and active; when you click ANOTHER window, it stops
//    staying on top (it moves to the background);
//  - when expanding/collapsing (the width changes), it grows equally on both sides.
// ENVIRONMENT (title "Jarvis Ambient <monitor>"):
//  - covers the monitor, stays below everything, and is excluded from the taskbar.
// CONTEXT:
//  - detects, per monitor, whether the desktop is clear (no large windows)
//    open) and notifies the interface over D-Bus (org.jarvisb.ui).
// Logs: journalctl --user -b | grep jarvisb

var MARGEM_TOPO_FRACAO = 0.06;   // top margin (6% ≈ 65 px at 1080p)
var MARGEM_TOPO_MINIMA = 48;
var WINDOW_OF_AJUSTE_MS = 1500;
var MAX_TENTATIVAS = 10;
var GRACA_ATIVACAO_MS = 2000;
var THRESHOLD_BUSY = 0.25;       // window covering >=25% of the monitor = occupied
var PREFIX_ENVIRONMENT = "Jarvis Ambient ";

var record = [];               // active panels: { w, nascida (creation time) }
var lastContext = "";

function legenda(w) {
    try { return w.caption || ""; } catch (e) { return ""; }
}
function ehPopup(w) { return legenda(w) === "Jarvis"; }
function ehEnvironment(w) { return legenda(w).indexOf(PREFIX_ENVIRONMENT) === 0; }
function ehBackground(w) { return legenda(w).indexOf("Jarvis Fundo ") === 0; }
function ehProprio(w) { return ehPopup(w) || ehEnvironment(w) || ehBackground(w); }
function ehJarvis(w) { return !!w && ehPopup(w); }

// ---------------------------------------------------------------
// Context: clear vs. occupied monitors
// ---------------------------------------------------------------

function relevante(w) {
    try {
        if (!w || ehProprio(w)) return false;
        if (w.desktopWindow || w.dock) return false;
        if (!w.normalWindow) return false;
        // System surfaces (including layer-shell, such as Jarvis’s own
        // background) do not appear in the taskbar.
        if (w.skipTaskbar) return false;
        if (w.minimized) return false;
        // "Show Desktop" (Meta+D) hides windows WITHOUT
        // minimizing them.
        if (workspace.showingDesktop) return false;
        if (!(w.onAllDesktops || w.desktops.indexOf(workspace.currentDesktop) >= 0)) return false;
        return true;
    } catch (e) { return false; }
}

function recalcular() {
    try {
        var outputs = workspace.screens;
        var janelas = workspace.windowList();
        var list = [];

        for (var i = 0; i < outputs.length; i++) {
            var s = outputs[i];
            var g = s.geometry;
            var total = g.width * g.height;
            var maior = 0;
            var por = "";

            for (var j = 0; j < janelas.length; j++) {
                var w = janelas[j];
                if (!relevante(w)) continue;

                var f = w.frameGeometry;
                var ix = Math.min(f.x + f.width, g.x + g.width) - Math.max(f.x, g.x);
                var iy = Math.min(f.y + f.height, g.y + g.height) - Math.max(f.y, g.y);

                if (ix > 0 && iy > 0 && ix * iy > maior) {
                    maior = ix * iy;
                    por = (w.resourceClass || legenda(w) || "?") + "";
                }
            }

            list.push({
                name: s.name,
                free: total > 0 && (maior / total) < THRESHOLD_BUSY,
                por: por
            });
        }

        var json = JSON.stringify({ outputs: list });
        if (json === lastContext) return;
        lastContext = json;

        // The popup (Python) and background (C++) receive the same context.
        callDBus("org.jarvisb.ui", "/ui", "org.jarvisb.Ui", "context", json);
        callDBus("org.jarvisb.background", "/background", "org.jarvisb.Background", "context", json);
        print("jarvisb: contexto " + json);
    } catch (e) {
        print("jarvisb: erro no contexto: " + e);
    }
}

function ligarContext(w) {
    var sinais = ["minimizedChanged", "frameGeometryChanged", "desktopsChanged",
                  "captionChanged", "outputChanged"];
    for (var i = 0; i < sinais.length; i++) {
        try { w[sinais[i]].connect(recalcular); } catch (e) {}
    }
}

// ---------------------------------------------------------------
// Panel (popup)
// ---------------------------------------------------------------

function observar(window) {
    var input = { w: window, nascida: Date.now() };
    record.push(input);

    var output = null;            // cursor monitor (fixed)
    var tentativas = 0;
    var elevada = false;
    var movido = false;
    var widthPrevious = 0;

    try {
        window.interactiveMoveResizeStarted.connect(function () { movido = true; });
    } catch (e) {}

    function tryPosition() {
        try {
            if (!ehJarvis(window)) return;

            var g = window.frameGeometry;
            if (!(g.width > 0) || !(g.height > 0)) return;

            if (!elevada) {
                elevada = true;
                window.keepAbove = true;
                workspace.activeWindow = window;
            }

            if (output === null) {
                output = (typeof workspace.screenAt === "function")
                    ? workspace.screenAt(workspace.cursorPos)
                    : workspace.activeScreen;
            }

            var area = workspace.clientArea(
                KWin.PlacementArea, output, workspace.currentDesktop
            );

            var inAjuste = (Date.now() - input.nascida) <= WINDOW_OF_AJUSTE_MS
                           && tentativas < MAX_TENTATIVAS;

            if (inAjuste) {
                var x = Math.round(area.x + (area.width - g.width) / 2);
                var y = Math.round(
                    area.y + Math.max(MARGEM_TOPO_MINIMA, area.height * MARGEM_TOPO_FRACAO)
                );

                if (isFinite(x) && isFinite(y)
                    && (Math.abs(g.x - x) > 1 || Math.abs(g.y - y) > 1)) {
                    tentativas++;
                    // "Qt" is not available in KWin scripts: modify a copy
                    // of the geometry and assign it back.
                    var nova = window.frameGeometry;
                    nova.x = x;
                    nova.y = y;
                    window.frameGeometry = nova;
                    print("jarvisb: painel movido para " + x + "," + y + " (tentativa " + tentativas + ")");
                }

                widthPrevious = window.frameGeometry.width;
                return;
            }

            // After the initial adjustment: only track width changes.
            if (!movido && widthPrevious > 0
                && Math.abs(g.width - widthPrevious) > 1) {
                var nx = Math.round(g.x - (g.width - widthPrevious) / 2);
                nx = Math.max(area.x + 8, Math.min(nx, area.x + area.width - g.width - 8));

                if (Math.abs(nx - g.x) > 1) {
                    var cop = window.frameGeometry;
                    cop.x = nx;
                    window.frameGeometry = cop;
                    print("jarvisb: largura mudou, painel em x=" + nx);
                }
            }

            widthPrevious = window.frameGeometry.width;
        } catch (e) {
            print("jarvisb: erro no script: " + e);
        }
    }

    window.captionChanged.connect(tryPosition);
    window.frameGeometryChanged.connect(tryPosition);
    tryPosition();
}

// ---------------------------------------------------------------
// Environment (one window per monitor, below everything)
// ---------------------------------------------------------------

function observarEnvironment(window) {
    var nascida = Date.now();
    var tentativas = 0;
    var preparada = false;

    function tryPosition() {
        try {
            if (!ehEnvironment(window)) return;

            if (!preparada) {
                preparada = true;
                window.keepBelow = true;
                window.skipTaskbar = true;
                window.skipPager = true;
                window.skipSwitcher = true;
                window.inBorder = true;
                print("jarvisb: ambiente preparado (" + legenda(window) + ")");
            }

            if ((Date.now() - nascida) > 3000 || tentativas >= MAX_TENTATIVAS) return;

            var name = legenda(window).substring(PREFIX_ENVIRONMENT.length);
            var outputs = workspace.screens;
            var target = null;
            for (var i = 0; i < outputs.length; i++) {
                if (outputs[i].name === name) target = outputs[i];
            }
            if (!target) return;

            var g = window.frameGeometry;
            var a = target.geometry;
            if (!(g.width > 0)) return;

            if (g.x !== a.x || g.y !== a.y || g.width !== a.width || g.height !== a.height) {
                tentativas++;
                var nova = window.frameGeometry;
                nova.x = a.x;
                nova.y = a.y;
                nova.width = a.width;
                nova.height = a.height;
                window.frameGeometry = nova;
                print("jarvisb: ambiente em " + name + " (" + a.x + "," + a.y + " " + a.width + "x" + a.height + ")");
            }
        } catch (e) {
            print("jarvisb: erro no ambiente: " + e);
        }
    }

    window.captionChanged.connect(tryPosition);
    window.frameGeometryChanged.connect(tryPosition);
    tryPosition();
}

// ---------------------------------------------------------------
// General connections
// ---------------------------------------------------------------

// Clicking another window -> the panel stops staying on top.
workspace.windowActivated.connect(function (active) {
    try {
        if (!active || ehProprio(active)) return;

        var now = Date.now();

        for (var i = 0; i < record.length; i++) {
            if (now - record[i].nascida > GRACA_ATIVACAO_MS) {
                try { record[i].w.keepAbove = false; } catch (e) {}
            }
        }
    } catch (e) {
        print("jarvisb: erro ao soltar o painel: " + e);
    }
});

workspace.windowAdded.connect(function (w) {
    try {
        ligarContext(w);
        observar(w);
        observarEnvironment(w);
        recalcular();
    } catch (e) {
        print("jarvisb: erro em windowAdded: " + e);
    }
});

workspace.windowRemoved.connect(function (w) {
    record = record.filter(function (r) { return r.w !== w; });
    recalcular();
});

try { workspace.currentDesktopChanged.connect(recalcular); } catch (e) {}
try { workspace.showingDesktopChanged.connect(recalcular); } catch (e) {}
try { workspace.screensChanged.connect(recalcular); } catch (e) {}

// Windows that already existed when the script was loaded.
try {
    var existentes = workspace.windowList();
    for (var k = 0; k < existentes.length; k++) ligarContext(existentes[k]);
} catch (e) {}

recalcular();
print("jarvisb: script carregado");