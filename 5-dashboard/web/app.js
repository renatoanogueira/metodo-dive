"use strict";
// Dashboard DIVE. Todo dato de AWS se escribe con textContent/createElement: nunca innerHTML con datos.
(function () {
  // El token llega en el fragmento (#t=...), que el navegador no envía a ningún servidor. Se guarda en la
  // sesión de esta pestaña y se borra de la URL (historial, capturas de pantalla).
  var TOKEN = null;
  var m = /(?:^#|&)t=([A-Za-z0-9_-]+)/.exec(location.hash);
  try {
    if (m) sessionStorage.setItem("dive-token", m[1]);
    TOKEN = (m && m[1]) || sessionStorage.getItem("dive-token");
  } catch (e) { TOKEN = m && m[1]; }
  if (m) history.replaceState(null, "", location.pathname);
  var NS = "http://www.w3.org/2000/svg";
  var estado = { org: null, run: null, cuenta: "", orgs: [], corridas: [], resumen: null, hallazgos: [],
                 recursos: [], pagina: 0, abierto: null };
  var POR_PAGINA = 100;
  var generacion = 0;  // cada carga se numera: la respuesta de una carga vieja no pinta encima de la actual

  // ---------- utilidades ----------

  function $(id) { return document.getElementById(id); }
  function el(tag, props, hijos) {
    var e = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === "text") e.textContent = props[k];
      else if (k === "class") e.className = props[k];
      else e.setAttribute(k, props[k]);
    });
    (hijos || []).forEach(function (h) { if (h != null) e.appendChild(typeof h === "string" ? document.createTextNode(h) : h); });
    return e;
  }
  function svg(tag, attrs) {
    var e = document.createElementNS(NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    return e;
  }
  // Moneda de la factura (discovery.finops.moneda_pago): los datos están en USD y se convierten solo al
  // mostrarlos, con la tasa de la última factura. estado.verUsd = el usuario prefirió ver USD.
  var SIMBOLOS = { USD: "$", EUR: "€", GBP: "£", BRL: "R$", JPY: "¥", INR: "₹", AUD: "A$", CAD: "C$" };
  function monedaVista() {
    var m = estado.resumen && estado.resumen.run && estado.resumen.run.moneda;
    return (m && m.codigo !== "USD" && !estado.verUsd) ? m : null;
  }
  function codigoVista() {
    var m = monedaVista();
    return m ? m.codigo : "USD";
  }
  function dinero(v, decimales) {
    if (v == null || isNaN(v)) return "—";
    var d = decimales == null ? 2 : decimales;
    var m = monedaVista();
    var n = Number(v) * (m ? m.tasa : 1);
    var s = m ? (SIMBOLOS[m.codigo] || m.codigo + " ") : "$";
    return (n < 0 ? "−" : "") + s + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  function pintarMoneda() {
    var m = estado.resumen && estado.resumen.run && estado.resumen.run.moneda, sel = $("sel-moneda");
    $("th-mes").textContent = (SIMBOLOS[codigoVista()] || codigoVista() + " ") + "/mes";
    $("l-moneda").hidden = !(m && m.codigo !== "USD");
    if ($("l-moneda").hidden) return;
    sel.textContent = "";
    var origen = m.fuente === "invoice" ? "factura " + (m.periodo || "") : "config";
    sel.appendChild(el("option", { value: m.codigo, text: m.codigo + " (1 USD = " + m.tasa.toFixed(4) + ", " + origen + ")" }));
    sel.appendChild(el("option", { value: "USD", text: "USD (sin convertir)" }));
    sel.value = estado.verUsd ? "USD" : m.codigo;
  }
  function sevClase(s) { return ["alta", "media", "baja"].indexOf(s) >= 0 ? s : "baja"; }
  function nombreCuenta(id) {
    var c = (estado.resumen && estado.resumen.cuentas) || {};
    return id ? (c[id] ? c[id] + " (" + id + ")" : id) : "varias";
  }
  function corridaCorta(id) { return id ? id.replace("T", " ").replace(/(\d{2})(\d{2})(\d{2})Z$/, "$1:$2 UTC") : "—"; }

  function api(ruta, opciones) {
    var o = opciones || {};
    o.headers = Object.assign({ "X-DIVE-Token": TOKEN }, o.headers || {});
    o.credentials = "omit";
    return fetch(ruta, o).then(function (r) {
      if (!r.ok) return r.text().then(function (t) { throw new Error(r.status + " " + t); });
      return r.json();
    });
  }
  function q(params) {
    return Object.keys(params).map(function (k) { return k + "=" + encodeURIComponent(params[k]); }).join("&");
  }

  // ---------- tooltip ----------

  var tip = $("tooltip");
  function mostrarTip(ev, lineas) {
    tip.textContent = "";
    lineas.forEach(function (l, i) { tip.appendChild(i === 0 ? el("b", { text: l }) : el("div", { text: l })); });
    tip.hidden = false;
    var x = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
    var y = Math.min(ev.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
    tip.style.left = x + "px";
    tip.style.top = y + "px";
  }
  function ocultarTip() { tip.hidden = true; }

  // ---------- gráficos (SVG simple) ----------

  // Barras apiladas por mes: un color fijo por cuenta (orden de la cuenta, nunca por ranking).
  function barrasApiladas(filas, meses, cuentas) {
    // El color sale del orden de TODAS las cuentas de la corrida: filtrar una no la repinta.
    var todas = Object.keys(cuentas).sort();
    var color = {};
    todas.forEach(function (c, i) { color[c] = "var(--series-" + ((i % 8) + 1) + ")"; });
    var presentes = {};
    filas.forEach(function (f) { presentes[f.account_id] = 1; });
    var orden = todas.filter(function (c) { return presentes[c]; });
    var porMes = {};
    meses.forEach(function (m) { porMes[m] = {}; });
    filas.forEach(function (f) { if (porMes[f.month]) porMes[f.month][f.account_id] = f.usd; });
    var totales = meses.map(function (m) { return orden.reduce(function (s, c) { return s + (porMes[m][c] || 0); }, 0); });
    var max = Math.max.apply(null, totales.concat([1]));
    var W = 1200, H = 280, izq = 64, abajo = 24, arriba = 10, ancho = (W - izq - 8) / Math.max(meses.length, 1);
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "Gasto mensual por cuenta" });
    s.style.width = "100%";
    [0, 0.5, 1].forEach(function (p) {
      var y = H - abajo - p * (H - abajo - arriba);
      s.appendChild(svg("line", { x1: izq, x2: W - 4, y1: y, y2: y, class: p === 0 ? "eje" : "grilla" }));
      var t = svg("text", { x: izq - 6, y: y + 4, "text-anchor": "end" });
      t.textContent = dinero(max * p, 0);
      s.appendChild(t);
    });
    meses.forEach(function (m, i) {
      var x = izq + i * ancho + ancho * 0.18, w = ancho * 0.64, base = H - abajo;
      orden.forEach(function (c) {
        var v = porMes[m][c] || 0;
        if (!v) return;
        var h = v / max * (H - abajo - arriba);
        var r = svg("rect", { x: x, y: base - h, width: w, height: Math.max(h - 2, 0.5), rx: 2 });
        r.style.fill = color[c];
        r.addEventListener("mousemove", function (ev) {
          mostrarTip(ev, [m, nombreCuenta(c) + ": " + dinero(v), "Total del mes: " + dinero(totales[i])]);
        });
        r.addEventListener("mouseleave", ocultarTip);
        s.appendChild(r);
        base -= h;
      });
      var t = svg("text", { x: x + w / 2, y: H - 6, "text-anchor": "middle" });
      t.textContent = m;
      s.appendChild(t);
    });
    var leyenda = el("div", { class: "leyenda" });
    if (orden.length < 2) return el("div", {}, [s]);  // una sola serie: el título ya la nombra
    orden.forEach(function (c) {
      var sp = el("span", { text: cuentas[c] || c });
      sp.style.setProperty("--c", color[c]);
      leyenda.appendChild(sp);
    });
    return el("div", {}, [leyenda, s]);
  }

  // Barras horizontales de una sola serie: sin leyenda, valor como etiqueta directa.
  function barrasHorizontales(filas, clave, titulo) {
    if (!filas.length) return el("div", { class: "vacio", text: "Sin datos" });
    var max = Math.max.apply(null, filas.map(function (f) { return f.usd; }).concat([1]));
    var fila = 22, W = 560, etiqueta = 230, H = filas.length * fila + 4;
    var s = svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": titulo });
    s.style.width = "100%";
    filas.forEach(function (f, i) {
      var y = i * fila + 2, w = Math.max(f.usd / max * (W - etiqueta - 70), 1);
      var nombre = String(f[clave] || "—");
      var t = svg("text", { x: etiqueta - 6, y: y + 14, "text-anchor": "end" });
      t.textContent = nombre.length > 34 ? nombre.slice(0, 33) + "…" : nombre;
      s.appendChild(t);
      var r = svg("rect", { x: etiqueta, y: y + 3, width: w, height: fila - 8, rx: 2 });
      r.style.fill = "var(--series-1)";
      var titulo = svg("title");
      titulo.textContent = nombre + ": " + dinero(f.usd);
      r.appendChild(titulo);
      r.addEventListener("mousemove", function (ev) { mostrarTip(ev, [nombre, dinero(f.usd) + " en el mes"]); });
      r.addEventListener("mouseleave", ocultarTip);
      s.appendChild(r);
      var v = svg("text", { x: etiqueta + w + 6, y: y + 14 });
      v.textContent = dinero(f.usd, 0);
      s.appendChild(v);
    });
    return s;
  }

  // ---------- vistas ----------

  var NOMBRE_CARGO = {
    Tax: "Impuestos", SavingsPlanRecurringFee: "Savings Plans: cuota mensual",
    SavingsPlanUpfrontFee: "Savings Plans: pago adelantado",
    SavingsPlanNegation: "Savings Plans: descuento sobre el uso cubierto",
    Recurring: "Reservas (RI): cuota mensual", Fee: "Reservas (RI) y otros: pago único",
    RIFee: "Reservas (RI): cuota", Credit: "Créditos", Refund: "Reembolsos", BundledDiscount: "Descuentos",
    Discount: "Descuentos"
  };

  function tablaFactura(r) {
    var cuerpo = el("tbody");
    var uso = r.gasto_ultimo_mes;
    cuerpo.appendChild(el("tr", {}, [el("td", { text: "Uso a precio de lista (servicios)" }), el("td", { class: "num", text: dinero(uso) })]));
    r.cargos.forEach(function (c) {
      cuerpo.appendChild(el("tr", {}, [el("td", { text: (NOMBRE_CARGO[c.record_type] || c.record_type) + " (" + c.record_type + ")" }),
                                       el("td", { class: "num", text: dinero(c.usd) })]));
    });
    cuerpo.appendChild(el("tr", {}, [el("td", {}, [el("b", { text: "Factura del mes" })]),
                                     el("td", { class: "num" }, [el("b", { text: dinero(r.factura_ultimo_mes) })])]));
    return el("div", { class: "panel scroll" }, [el("table", { class: "tabla" }, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Concepto (" + (r.ultimo_mes || "") + ")" }), el("th", { class: "num", text: codigoVista() })])]), cuerpo])]);
  }

  function tablaMensual(r) {
    var uso = {}, cargos = {};
    r.por_cuenta_mes.forEach(function (x) { uso[x.month] = (uso[x.month] || 0) + x.usd; });
    r.cargos_mes.forEach(function (x) { cargos[x.month] = x.usd; });
    var cuerpo = el("tbody");
    r.meses.forEach(function (m) {
      cuerpo.appendChild(el("tr", {}, [el("td", { text: m }), el("td", { class: "num", text: dinero(uso[m] || 0, 0) }),
        el("td", { class: "num", text: dinero(cargos[m] || 0, 0) }), el("td", { class: "num", text: dinero((uso[m] || 0) + (cargos[m] || 0), 0) })]));
    });
    return el("div", { class: "panel scroll" }, [el("table", { class: "tabla" }, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Mes" }), el("th", { class: "num", text: "Uso (lista)" }),
        el("th", { class: "num", text: "Impuestos, compromisos y créditos" }), el("th", { class: "num", text: "Factura" })])]), cuerpo])]);
  }

  function tarjeta(k, v, n) {
    return el("div", { class: "card" }, [el("div", { class: "k", text: k }), el("div", { class: "v", text: v }),
                                         n ? el("div", { class: "n", text: n }) : null]);
  }

  function pintarResumen() {
    var r = estado.resumen, cont = $("t-resumen");
    cont.textContent = "";
    if (!r) { cont.appendChild(el("div", { class: "vacio", text: "Sin datos para esta corrida." })); return; }
    var aviso = r.run.sin_doctor ? " · corrida sin aws-doctor: el ahorro y los hallazgos son parciales" : "";
    var v = r.ventana, moneda = codigoVista();
    var servicios = {};
    r.recursos_por_servicio.forEach(function (x) { servicios[x.service] = 1; });
    cont.appendChild(el("h2", { class: "grupo", text: "Inventario" }));
    cont.appendChild(el("div", { class: "cards" }, [
      tarjeta("Cuentas", String(r.n_cuentas), r.cuenta ? nombreCuenta(r.cuenta)
              : r.n_cuentas_con_costo + " con costo en la ventana" + (r.n_cuentas > r.n_cuentas_con_costo ? "; el resto, solo inventario o cargos" : "")),
      tarjeta("Regiones", String(r.regiones.length), r.regiones.join(", ")),
      tarjeta("Recursos", r.n_recursos.toLocaleString("en-US"), Object.keys(servicios).length + " servicios")
    ]));
    cont.appendChild(el("h2", { class: "grupo", text: "Costo de la ventana (" + v.meses + " meses: " + (v.desde || "") + " → " + (v.hasta || "") + ")" }));
    cont.appendChild(el("div", { class: "cards" }, [
      r.tiene_cargos ? tarjeta("Factura total", dinero(v.factura), "uso + impuestos + compromisos, " + moneda) : null,
      tarjeta("Gasto (uso) total", dinero(v.uso), "a precio de lista, sin impuestos ni descuentos"),
      r.tiene_cargos ? tarjeta("Impuestos total", dinero(v.impuestos), "Tax en Cost Explorer") : null
    ]));
    cont.appendChild(el("h2", { class: "grupo", text: "Último mes (" + (r.ultimo_mes || "") + ")" }));
    cont.appendChild(el("div", { class: "cards" }, [
      r.tiene_cargos ? tarjeta("Factura", dinero(r.factura_ultimo_mes, 0), "con impuestos y compromisos, " + moneda) : null,
      r.tiene_cargos ? tarjeta("Impuestos", dinero(r.impuestos_ultimo_mes, 0), "Tax en Cost Explorer") : null,
      tarjeta("Uso", dinero(r.gasto_ultimo_mes, 0), "a precio de lista, sin impuestos ni descuentos")
    ]));
    cont.appendChild(el("h2", { class: "grupo", text: "Hallazgos" }));
    cont.appendChild(el("div", { class: "cards" }, [
      tarjeta("Ahorro potencial / mes", dinero(r.ahorro_abierto, 0), "estimado; sin aceptados ni ignorados" + aviso),
      r.ahorro_seguro_ya != null ? tarjeta("Ahorro seguro ya / mes", dinero(r.ahorro_seguro_ya, 0),
        "reversible y re-verificado por la skill metodo-dive") : null,
      tarjeta("Altos abiertos", String(r.altos_abiertos), r.abiertos + " hallazgos abiertos"),
      tarjeta("Cambios", "+" + r.nuevos + " / −" + r.resueltos,
              "nuevos / resueltos vs. corridas anteriores" + (r.no_verificados ? " · " + r.no_verificados + " sin verificar" : ""))
    ]));
    var prob = (r.insights && r.insights.problemas) || [];
    if (prob.length) {
      cont.appendChild(el("div", { class: "aviso-insights" }, [
        el("b", { text: "insights.json no es válido y no se muestra:" }),
        el("ul", {}, prob.slice(0, 10).map(function (p) { return el("li", { text: p }); }))]));
    }
    if (r.prioridades && r.prioridades.length) {
      cont.appendChild(el("h2", { class: "grupo", text: "Lo primero que concluye la skill" }));
      r.prioridades.forEach(function (ins) { cont.appendChild(tarjetaInsightCorta(ins)); });
      var ver = el("button", { type: "button", class: "btn", text: "Ver los hallazgos con su insight" });
      ver.addEventListener("click", function () { document.querySelector('nav button[data-tab="hallazgos"]').click(); });
      cont.appendChild(ver);
    }
    if (r.aparte && r.aparte.length) {
      cont.appendChild(el("h2", { class: "grupo", text: "Lo que la skill anota aparte (" + r.aparte.length + ")" }));
      r.aparte.forEach(function (ins) { cont.appendChild(tarjetaInsight(ins)); });
    }
    if (r.tiene_cargos) {
      cont.appendChild(el("div", { class: "grid2" }, [
        el("div", {}, [el("h2", { text: "Factura del mes por concepto" }), tablaFactura(r)]),
        el("div", {}, [el("h2", { text: "Factura mes a mes" }), tablaMensual(r)])]));
    } else {
      cont.appendChild(el("p", { class: "hint", text: "Esta corrida no trae impuestos ni cargos de compromisos (es anterior a esa consulta, o la consulta falló: ver errores de la corrida). Solo uso a precio de lista." }));
    }
    cont.appendChild(el("h2", { text: "Uso mensual por cuenta (precio de lista)" }));
    cont.appendChild(el("div", { class: "panel pad" }, [barrasApiladas(r.por_cuenta_mes, r.meses, r.cuentas)]));
    cont.appendChild(el("div", { class: "grid2" }, [
      el("div", {}, [el("h2", { text: "Servicios más caros (" + (r.ultimo_mes || "") + ")" }),
                     el("div", { class: "panel pad" }, [barrasHorizontales(r.servicios, "service", "Servicios")])]),
      el("div", {}, [el("h2", { text: "Tipos de uso más caros (" + (r.ultimo_mes || "") + ")" }),
                     el("div", { class: "panel pad" }, [barrasHorizontales(r.usos, "usage_type", "Tipos de uso")]),
                     el("p", { class: "hint", text: "Gasto real por USAGE_TYPE: dónde mirar (NAT, transferencia, almacenamiento, métricas). No es ahorro." })])
    ]));
    cont.appendChild(el("h2", { text: "Recursos por servicio y tipo" }));
    var cuerpoRec = el("tbody");
    r.recursos_por_servicio.forEach(function (x) {
      var tr = el("tr", { class: "clic" }, [el("td", { text: x.service + " / " + x.type }), el("td", { class: "num", text: String(x.n) })]);
      tr.addEventListener("click", function () { irARecursos(x.service, x.type); });
      cuerpoRec.appendChild(tr);
    });
    if (!r.recursos_por_servicio.length) cuerpoRec.appendChild(el("tr", {}, [el("td", { colspan: "2", class: "vacio", text: "Sin recursos" })]));
    cont.appendChild(el("div", { class: "panel scroll" }, [el("table", { class: "tabla" }, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Servicio / tipo" }), el("th", { class: "num", text: "Recursos" })])]), cuerpoRec])]));
    cont.appendChild(el("h2", { text: "Evolución entre corridas" }));
    var cuerpo = el("tbody");
    r.evolucion.forEach(function (c) {
      cuerpo.appendChild(el("tr", {}, [el("td", { text: corridaCorta(c.run_id) }),
        el("td", { text: c.sin_doctor ? "sin aws-doctor" : "completa" }),
        el("td", { class: "num", text: String(c.n_findings) }), el("td", { class: "num", text: dinero(c.ahorro) })]));
    });
    cont.appendChild(el("div", { class: "panel scroll" }, [el("table", { class: "tabla" }, [
      el("thead", {}, [el("tr", {}, [el("th", { text: "Corrida" }), el("th", { text: "Tipo" }),
        el("th", { class: "num", text: "Hallazgos" }), el("th", { class: "num", text: "Ahorro estimado/mes" })])]), cuerpo])]));
  }

  // Filtros de Recursos y Hallazgos y pestaña activa: sobreviven a un refresh (solo en este navegador).
  var IDS_FILTROS = ["f-presencia", "f-estado", "f-sev", "f-cat", "f-verif", "f-q", "r-reg", "r-svc", "r-tipo", "r-hall", "r-q"];
  var pendientes = {};
  // Hay filtros guardados de otra sesión: se avisa cuántos están activos (el de presencia empieza en "presente").
  var DEFECTO = { "f-presencia": "presente" };
  function pintarFiltrosActivos() {
    var caja = $("f-activos"), n = 0;
    ["f-presencia", "f-estado", "f-sev", "f-cat", "f-verif", "f-q"].forEach(function (id) {
      if ($(id).value !== (DEFECTO[id] || "")) n++;
    });
    caja.textContent = "";
    caja.hidden = !n;
    if (!n) return;
    var limpiar = el("button", { type: "button", class: "enlace", text: "limpiar" });
    limpiar.addEventListener("click", function () {
      ["f-presencia", "f-estado", "f-sev", "f-cat", "f-verif", "f-q"].forEach(function (id) { $(id).value = DEFECTO[id] || ""; });
      guardarFiltros(); pintarHallazgos();
    });
    caja.appendChild(document.createTextNode(n + " filtro(s) activo(s) · "));
    caja.appendChild(limpiar);
  }

  function guardarFiltros() {
    var d = {};
    IDS_FILTROS.forEach(function (id) { d[id] = $(id).value; });
    try { localStorage.setItem("dive-filtros", JSON.stringify(d)); } catch (e) { /* sin storage */ }
  }
  function restaurarFiltros() {
    var d = {};
    try { d = JSON.parse(localStorage.getItem("dive-filtros") || "{}") || {}; } catch (e) { /* sin storage o corrupto */ }
    IDS_FILTROS.forEach(function (id) {
      var v = d[id];
      if (typeof v !== "string") return;
      var c = $(id);
      if (c.tagName === "INPUT") { c.value = v; return; }
      c.value = v;                                   // opciones fijas: se aplica si existe
      if (c.value !== v) pendientes[id] = v;         // opciones dinámicas: se aplica cuando se llenen
    });
  }
  // Valor a conservar al rellenar un select dinámico: el actual o, la primera vez, el guardado.
  function valorActual(s) {
    var v = s.value || pendientes[s.id] || "";
    delete pendientes[s.id];
    return v;
  }

  function filtrosHallazgos() {
    var cats = {};
    estado.hallazgos.forEach(function (f) { if (f.category) cats[f.category] = 1; });
    [["f-cat", Object.keys(cats).sort(), function (c) { return c; }]].forEach(function (d) {
      var s = $(d[0]), actual = valorActual(s);
      while (s.options.length > 1) s.remove(1);
      d[1].forEach(function (v) { s.appendChild(el("option", { value: v, text: d[2](v) })); });
      s.value = d[1].indexOf(actual) >= 0 ? actual : "";
    });
  }

  var ESTADOS_INSIGHT = { seguro_ya: "seguro ya", firma: "necesita firma", contradicho: "contradicho",
                          no_verificado: "no se pudo verificar", informativo: "informativo" };
  var TIPOS_INSIGHT = { hallazgo: "hallazgo", causa_costo: "causa de costo", recomendacion: "recomendación",
                        pregunta: "pregunta", laguna: "lo que no se pudo ver" };

  // El texto de un insight trae {{c1}}: el dinero y los porcentajes llegan como números con su formato y se
  // pintan aquí (el dinero con dinero(): sigue el selector de moneda). Nada del texto se interpreta como HTML.
  function textoInsight(t, ins) {
    return String(t || "").replace(/\{\{(c\d{1,3})\}\}/g, function (m, c) {
      var d = (ins.cifras || {})[c], v = (ins.valores || {})[c];
      if (!d || v == null) return m;
      return d.formato === "usd" ? dinero(v) : d.formato === "pct" ? Number(v).toFixed(1) + "%" : String(Number(v));
    });
  }

  // Cada estado filtra solo los insights de ESTA corrida (como el total "Ahorro seguro ya"); los de una corrida
  // anterior tienen su propia opción.
  function pasaVerif(f, ver) {
    if (!ver) return true;
    var v = f.insight;
    if (ver === "sin") return !v;
    if (ver === "vieja") return !!v && !v.vigente;
    return !!v && v.vigente && v.estado === ver;
  }

  function evidenciaInsight(ins) {
    return (ins.evidencia || []).map(function (e) {
      var vals = e.valores ? " · " + Object.keys(e.valores).map(function (k) { return k + " = " + e.valores[k]; }).join(", ") : "";
      return el("div", { class: "detalle" }, [
        el("span", { text: "[" + e.id + "] " + (e.origen === "aws" ? "consulta a AWS: " : "datos de DIVE: ") + e.descripcion + vals }),
        e.comando ? el("div", {}, [el("code", { text: e.comando })]) : null
      ]);
    });
  }

  // Detalle completo de un insight: texto, lo que no se pudo determinar, la pregunta al dueño, la evidencia y la acción.
  function detalleInsight(ins, sinAviso) {
    var fps = (ins.alcance || {}).fingerprints || [];
    var aviso = null;
    if (!sinAviso && fps.length > 1) {
      var cuentas = {};
      estado.hallazgos.forEach(function (f) { if (fps.indexOf(f.fingerprint) >= 0) cuentas[f.account_id] = 1; });
      var n = Object.keys(cuentas).length;
      aviso = el("div", { class: "detalle", text: "Abarca " + fps.length + " recursos" + (n > 1 ? " de " + n + " cuentas" : "") +
        ": el monto del texto es el del conjunto, no el de esta fila." });
    }
    return el("div", { class: "insight-detalle" }, [
      el("p", { text: textoInsight(ins.texto, ins) }), aviso,
      ins.no_determinado ? el("div", { class: "indeterminado", text: "No determinado: " + ins.no_determinado }) : null,
      ins.pregunta_dueno ? el("div", { class: "pregunta", text: "Pregunta para el dueño: " + ins.pregunta_dueno }) : null
    ].concat(evidenciaInsight(ins)).concat([
      ins.accion ? el("div", { class: "accion" }, [
        el("div", { class: "detalle", text: "Acción (la ejecuta una persona)" + (ins.accion.reversibilidad ? " · " + ins.accion.reversibilidad : "") }),
        el("pre", { text: ins.accion.comando })]) : null
    ]));
  }

  // Dentro de cada hallazgo: una línea con el estado y el título del insight, y "ver detalle" que despliega el resto.
  // Uno de una corrida anterior se muestra atenuado: el recurso pudo cambiar y no cuenta en el total.
  function bloqueInsight(f) {
    var v = f.insight;
    if (!v) return null;
    var cuerpo = null;
    var ver = el("button", { type: "button", class: "enlace", text: "ver detalle", "aria-expanded": "false" });
    var caja = el("div", { class: "verif" + (v.vigente ? "" : " vieja") }, [
      el("span", { class: "tag v-" + (v.vigente ? v.estado : "vieja"),
                   text: "skill: " + (ESTADOS_INSIGHT[v.estado] || v.estado) + (v.vigente ? "" : " (corrida anterior)") }),
      el("span", { text: " " + textoInsight(v.titulo, v) + (v.vigente ? "" : " · de " + corridaCorta(v.corrida) + ", re-analizar") +
        (((v.alcance || {}).fingerprints || []).length > 1 ? " (conjunto de " + v.alcance.fingerprints.length + ")" : "") + " · " }),
      ver
    ]);
    ver.addEventListener("click", function () {
      if (cuerpo) { cuerpo.remove(); cuerpo = null; ver.textContent = "ver detalle"; ver.setAttribute("aria-expanded", "false"); return; }
      cuerpo = detalleInsight(v);
      caja.appendChild(cuerpo);
      ver.textContent = "ocultar detalle"; ver.setAttribute("aria-expanded", "true");
    });
    return caja;
  }

  // Tarjeta de la pestaña Resumen: estado, título y detalle completo (para lo que no cuelga de ningún hallazgo).
  function tarjetaInsight(ins) {
    return el("div", { class: "insight est-" + ins.estado }, [
      el("div", { class: "cab" }, [
        el("span", { class: "tag v-" + ins.estado, text: ESTADOS_INSIGHT[ins.estado] || ins.estado }),
        el("b", { text: textoInsight(ins.titulo, ins) })]),
      detalleInsight(ins, true)
    ]);
  }

  // Versión corta para "Lo primero que concluye la skill": estado, título y texto.
  function tarjetaInsightCorta(ins) {
    return el("div", { class: "insight est-" + ins.estado }, [
      el("div", { class: "cab" }, [
        el("span", { class: "tag v-" + ins.estado, text: ESTADOS_INSIGHT[ins.estado] || ins.estado }),
        el("b", { text: textoInsight(ins.titulo, ins) })]),
      el("p", { text: textoInsight(ins.texto, ins) }),
      ins.pregunta_dueno ? el("div", { class: "pregunta", text: "Pregunta para el dueño: " + ins.pregunta_dueno }) : null
    ]);
  }

  function pintarHallazgos() {
    pintarFiltrosActivos();
    var pres = $("f-presencia").value, est = $("f-estado").value, sev = $("f-sev").value,
        cat = $("f-cat").value, ver = $("f-verif").value, cta = estado.cuenta, txt = $("f-q").value.toLowerCase();
    var lista = estado.hallazgos.filter(function (f) {
      if (!pasaVerif(f, ver)) return false;
      if (pres === "nuevo" ? !f.nuevo : pres && f.presencia !== pres) return false;
      if (est && f.estado !== est) return false;
      if (sev && f.severity !== sev) return false;
      if (cat && f.category !== cat) return false;
      if (cta && f.account_id !== cta) return false;
      if (txt && [f.title, f.detail, f.resource_id, f.rule_id].join(" ").toLowerCase().indexOf(txt) < 0) return false;
      return true;
    });
    // Contador por severidad: con todos los filtros salvo el de severidad (como en el reporte); el chip filtra.
    var porSev = { alta: 0, media: 0, baja: 0 };
    estado.hallazgos.forEach(function (f) {
      if (pres === "nuevo" ? !f.nuevo : pres && f.presencia !== pres) return;
      if ((est && f.estado !== est) || (cat && f.category !== cat) || (cta && f.account_id !== cta)) return;
      if (!pasaVerif(f, ver)) return;
      if (txt && [f.title, f.detail, f.resource_id, f.rule_id].join(" ").toLowerCase().indexOf(txt) < 0) return;
      if (porSev[f.severity] != null) porSev[f.severity]++;
    });
    var chips = $("h-sev");
    chips.textContent = "";
    ["alta", "media", "baja"].forEach(function (s) {
      var b = el("button", { type: "button", class: "chip sev sev-" + s, "aria-pressed": String(sev === s),
                             text: porSev[s] + " " + s });
      b.addEventListener("click", function () { $("f-sev").value = sev === s ? "" : s; pintarHallazgos(); });
      chips.appendChild(b);
    });
    var ahorro = lista.reduce(function (s, f) {
      return s + (f.is_saving && f.presencia === "presente" && f.estado !== "aceptado" && f.estado !== "ignorado" ? (f.monthly_usd || 0) : 0);
    }, 0);
    $("h-meta").textContent = lista.length + " hallazgos" + (ahorro ? " · ahorro estimado " + dinero(ahorro) + "/mes" : "");
    var cuerpo = $("h-body");
    cuerpo.textContent = "";
    if (!lista.length) cuerpo.appendChild(el("tr", {}, [el("td", { colspan: "7", class: "vacio", text: "Ningún hallazgo con estos filtros" })]));
    lista.forEach(function (f) {
      var historia = el("td", {}, [
        f.nuevo ? el("span", { class: "tag nuevo", text: "nuevo" }) : null,
        f.reaparece ? el("span", { class: "tag reaparece", text: "reaparece" }) : null,
        f.presencia !== "presente" ? el("span", { class: "tag", text: f.presencia === "resuelto"
          ? "ya no aparece desde " + corridaCorta(f.resuelto_en) : "no verificado en esta corrida" }) : null,
        el("div", { class: "detalle", text: "desde " + corridaCorta(f.primera) + " · " + f.veces + " corrida(s)" })
      ]);
      cuerpo.appendChild(el("tr", {}, [
        el("td", {}, [el("span", { class: "sev sev-" + sevClase(f.severity), text: f.severity })]),
        el("td", {}, [el("div", { text: f.title }), el("div", { class: "detalle", text: f.detail || "" }),
                      el("div", { class: "detalle", text: f.category + " · " + f.source + " · " + (f.rule_id || "") }),
                      bloqueInsight(f)]),
        el("td", { text: nombreCuenta(f.account_id) + (f.region ? " · " + f.region : "") }),
        el("td", { class: "detalle", text: f.resource_id || "—" }),
        el("td", { class: "num", text: f.monthly_usd != null ? dinero(f.monthly_usd) + (f.is_saving === false && (f.category === "costo" || f.category === "desperdicio") ? " (no suma)" : "") : "—" }),
        historia,
        celdaEstado(f)
      ]));
    });
  }

  function celdaEstado(f) {
    var sel = el("select", { "aria-label": "Estado de: " + f.title });
    ["abierto", "aceptado", "ignorado", "resuelto"].forEach(function (s) { sel.appendChild(el("option", { value: s, text: s })); });
    sel.value = f.estado;
    var nota = el("textarea", { placeholder: "Nota (por qué se acepta, ticket...)", maxlength: "2000",
                                "aria-label": "Nota de: " + f.title });
    nota.value = f.nota || "";
    var ok = el("div", { class: "ok", text: f.estado_fecha ? "guardado " + f.estado_fecha : "" });
    var cola = Promise.resolve();  // los guardados de una fila van en orden: el último gana siempre
    function guardar() {
      var cuerpo = JSON.stringify({ org: estado.org, fingerprint: f.fingerprint, status: sel.value, nota: nota.value });
      ok.textContent = "guardando…";
      cola = cola.then(function () {
        return api("/api/estado", { method: "POST", headers: { "Content-Type": "application/json" }, body: cuerpo })
          .then(function (r) {
            f.estado = r.status; f.nota = r.nota; f.estado_fecha = r.updated_at; ok.textContent = "guardado " + r.updated_at;
            refrescarResumen();  // ahorro y altos abiertos excluyen aceptados/ignorados
          })
          .catch(function (e) { ok.textContent = "error: " + e.message; });
      });
    }
    sel.addEventListener("change", guardar);
    nota.addEventListener("change", guardar);
    return el("td", { class: "estado" }, [sel, nota, ok]);
  }

  function opciones(id, valores, etiqueta) {
    var s = $(id), actual = valorActual(s);
    while (s.options.length > 1) s.remove(1);
    valores.forEach(function (v) { s.appendChild(el("option", { value: v, text: etiqueta ? etiqueta(v) : v })); });
    s.value = valores.indexOf(actual) >= 0 ? actual : "";
  }

  function filtrosRecursos() {
    var reg = {}, svc = {}, tipo = {};
    estado.recursos.forEach(function (r) { reg[r.region] = 1; svc[r.service] = 1; tipo[r.type] = 1; });
    opciones("r-reg", Object.keys(reg).sort());
    opciones("r-svc", Object.keys(svc).sort());
    opciones("r-tipo", Object.keys(tipo).sort());
  }

  function irARecursos(servicio, tipo) {
    $("r-reg").value = ""; $("r-hall").value = ""; $("r-q").value = "";
    $("r-svc").value = servicio;
    $("r-tipo").value = tipo;
    estado.pagina = 0;
    pintarRecursos();
    document.querySelector('nav button[data-tab="recursos"]').click();
  }

  function textoBusqueda(r) {
    return [r.id, r.name, r.arn, r.state, JSON.stringify(r.tags), JSON.stringify(r.attrs)].join(" ").toLowerCase();
  }

  function bloque(titulo, obj) {
    var claves = Object.keys(obj || {});
    var texto = claves.length ? claves.map(function (k) {
      var v = obj[k];
      return k + ": " + (typeof v === "object" ? JSON.stringify(v) : String(v));
    }).join("\n") : "—";
    return el("div", {}, [el("h4", { text: titulo }), el("pre", { text: texto })]);
  }

  function pintarRecursos() {
    var reg = $("r-reg").value, svc = $("r-svc").value, tipo = $("r-tipo").value, hall = $("r-hall").value,
        txt = $("r-q").value.toLowerCase();
    var lista = estado.recursos.filter(function (r) {
      return (!reg || r.region === reg) && (!svc || r.service === svc) && (!tipo || r.type === tipo) &&
        (!hall || r.n_hallazgos > 0) && (!txt || textoBusqueda(r).indexOf(txt) >= 0);
    });
    var paginas = Math.max(1, Math.ceil(lista.length / POR_PAGINA));
    estado.pagina = Math.min(estado.pagina, paginas - 1);
    var desde = estado.pagina * POR_PAGINA, pagina = lista.slice(desde, desde + POR_PAGINA);
    $("r-meta").textContent = lista.length ? "Mostrando " + (desde + 1) + "–" + (desde + pagina.length) + " de " + lista.length + " recursos" : "0 recursos";
    var pag = $("r-pag");
    pag.textContent = "";
    pag.className = "pag";
    if (paginas > 1) {
      var ant = el("button", { class: "btn", type: "button", text: "‹ Anterior" });
      var sig = el("button", { class: "btn", type: "button", text: "Siguiente ›" });
      ant.disabled = estado.pagina === 0;
      sig.disabled = estado.pagina >= paginas - 1;
      ant.addEventListener("click", function () { estado.pagina--; pintarRecursos(); });
      sig.addEventListener("click", function () { estado.pagina++; pintarRecursos(); });
      pag.appendChild(ant);
      pag.appendChild(sig);
    }
    var cuerpo = $("r-body");
    cuerpo.textContent = "";
    if (!pagina.length) cuerpo.appendChild(el("tr", {}, [el("td", { colspan: "8", class: "vacio", text: "Ningún recurso con estos filtros" })]));
    pagina.forEach(function (r) {
      var clave = r.account_id + "|" + r.region + "|" + r.service + "|" + r.type + "|" + r.id;
      var tr = el("tr", { class: "clic", tabindex: "0" }, [
        el("td", { text: nombreCuenta(r.account_id) }), el("td", { text: r.region || "" }),
        el("td", { text: r.service || "" }), el("td", { text: r.type || "" }), el("td", { text: r.name || "—" }),
        el("td", { class: "detalle", text: r.id }), el("td", { text: r.state || "—" }),
        el("td", { class: "num", text: r.n_hallazgos ? String(r.n_hallazgos) : "" })]);
      function alternar() { estado.abierto = estado.abierto === clave ? null : clave; pintarRecursos(); }
      tr.addEventListener("click", alternar);
      tr.addEventListener("keydown", function (e) { if (e.key === "Enter") alternar(); });
      cuerpo.appendChild(tr);
      if (estado.abierto === clave) {
        var suyos = estado.hallazgos.filter(function (f) {
          return f.presencia === "presente" && f.account_id === r.account_id &&
            (f.resource_id === r.id || (r.arn && f.resource_id === r.arn));
        }).map(function (f) { return f.severity + " · " + f.title; });
        cuerpo.appendChild(el("tr", { class: "detalle-fila" }, [el("td", { colspan: "8" }, [el("div", { class: "kv" }, [
          bloque("Tags", r.tags), bloque("Atributos", r.attrs), bloque("Relaciones", r.relations),
          el("div", {}, [el("h4", { text: "Hallazgos" }), el("pre", { text: suyos.join("\n") || "—" })]),
          r.arn ? el("div", {}, [el("h4", { text: "ARN" }), el("pre", { text: r.arn })]) : null])])]));
      }
    });
  }

  function pintarCorridas() {
    var cont = $("t-corridas");
    cont.textContent = "";
    var cuerpo = el("tbody");
    estado.corridas.slice().reverse().forEach(function (c) {
      cuerpo.appendChild(el("tr", {}, [
        el("td", { text: corridaCorta(c.run_id) }),
        el("td", { text: c.sin_doctor ? "sin aws-doctor" : "completa" }),
        el("td", { class: "num", text: String(c.n_findings) }),
        el("td", { class: "num", text: dinero(c.ahorro) }),
        el("td", { class: "num", text: String(c.n_errores) }),
        el("td", { class: "detalle", text: c.dive_version || "" }),
        el("td", {}, [botonReporte(c.run_id)])
      ]));
    });
    cont.appendChild(el("div", { class: "panel scroll" }, [el("table", { class: "tabla" }, [
      el("thead", {}, [el("tr", {}, ["Corrida", "Tipo", "Hallazgos", "Ahorro/mes", "Errores", "Versión DIVE", "Inventario"].map(function (t, i) {
        return el("th", { class: i >= 2 && i <= 4 ? "num" : "", text: t });
      }))]), cuerpo])]));
    cont.appendChild(el("p", { class: "hint", text: "El reporte de cada corrida (recursos y relaciones) se abre abajo, aislado del dashboard." }));
    cont.appendChild(el("div", { id: "marco-reporte" }));
  }

  // El iframe no puede mandar el header: se pide un ticket de un solo uso y se abre en un sandbox sin salida.
  function botonReporte(runId) {
    var b = el("button", { class: "btn", type: "button", text: "Ver reporte" });
    b.addEventListener("click", function () {
      api("/api/ticket?" + q({ run: runId })).then(function (r) {
        var marco = $("marco-reporte");
        marco.textContent = "";
        marco.appendChild(el("iframe", { class: "reporte", title: "Reporte de la corrida " + runId,
          sandbox: "allow-scripts", src: "/reporte/" + encodeURIComponent(runId) + "?" + q({ k: r.ticket }) }));
      }).catch(fallo);
    });
    return b;
  }

  // ---------- carga ----------

  function refrescarResumen() {
    var base = { org: estado.org, run: estado.run };
    if (estado.cuenta) base.cuenta = estado.cuenta;
    api("/api/resumen?" + q(base)).then(function (r) { estado.resumen = r; pintarResumen(); }).catch(function () {});
  }

  // El informe de la skill se muestra como texto (textContent): no se interpreta Markdown ni HTML.
  function cargarInforme() {
    var pre = $("informe-texto"), mia = generacion;
    pre.textContent = "Cargando…";
    api("/api/informe?" + q({ org: estado.org, run: estado.run }))
      .then(function (r) {
        if (mia !== generacion) return;  // otra corrida ya elegida: no pintar
        pre.textContent = (r.actual ? "" : "AVISO: este informe no corresponde al insights.json actual (se editó después o no trae su marca). " +
                           "Vuelve a correr skill/scripts/insights.py.\n\n") + r.texto;
      })
      .catch(function (e) { if (mia === generacion) pre.textContent = "No se pudo cargar: " + e.message; });
  }

  function fallo(e) { $("sub").textContent = "Error al cargar: " + e.message; }

  function cargarRun() {
    var base = { org: estado.org, run: estado.run }, mia = ++generacion;
    if (estado.cuenta) base.cuenta = estado.cuenta;
    var p = q(base);
    return Promise.all([api("/api/resumen?" + p), api("/api/hallazgos?" + q({ org: estado.org, run: estado.run })),
                        api("/api/recursos?" + p)]).then(function (res) {
      if (mia !== generacion) return;
      estado.resumen = res[0];
      pintarMoneda();
      $("tab-informe").hidden = !res[0].tiene_informe;
      $("informe-texto").textContent = "";
      // La pestaña abierta era el Informe: recargar el de esta corrida, o volver a Resumen si ya no hay.
      if (!$("t-informe").hidden) {
        if (res[0].tiene_informe) cargarInforme(); else document.querySelector('nav button[data-tab="resumen"]').click();
      }
      estado.hallazgos = res[1];
      estado.recursos = res[2];
      estado.pagina = 0;
      var sc = $("sel-cuenta"), actual = estado.cuenta;
      while (sc.options.length > 1) sc.remove(1);
      Object.keys(res[0].cuentas).sort(function (a, b) { return String(res[0].cuentas[a]).localeCompare(String(res[0].cuentas[b])); })
        .forEach(function (a) { sc.appendChild(el("option", { value: a, text: res[0].cuentas[a] + " (" + a + ")" })); });
      sc.value = actual;
      if (actual && sc.value !== actual) {  // la cuenta no está en esta corrida: se vuelve a "todas"
        estado.cuenta = "";
        return cargarRun();
      }
      $("b-recursos").textContent = String(res[2].length);
      var org = estado.orgs.filter(function (o) { return o.org_id === estado.org; })[0] || {};
      $("sub").textContent = (org.alias || estado.org) + " · corrida " + corridaCorta(estado.run) +
        (res[0].run.sin_doctor ? " · sin aws-doctor" : "");
      $("b-hallazgos").textContent = String(res[1].filter(function (f) {
        return f.presencia === "presente" && (!estado.cuenta || f.account_id === estado.cuenta);
      }).length);
      filtrosHallazgos();
      filtrosRecursos();
      pintarResumen();
      pintarRecursos();
      pintarHallazgos();
      pintarCorridas();
    });
  }

  function cargarOrg() {
    var mia = ++generacion;
    return api("/api/corridas?" + q({ org: estado.org })).then(function (cs) {
      if (mia !== generacion) return;
      estado.corridas = cs;
      var s = $("sel-run");
      s.textContent = "";
      cs.slice().reverse().forEach(function (c) {
        s.appendChild(el("option", { value: c.run_id, text: corridaCorta(c.run_id) + (c.sin_doctor ? " (sin aws-doctor)" : "") }));
      });
      estado.run = cs.length ? cs[cs.length - 1].run_id : null;
      s.value = estado.run;
      return estado.run ? cargarRun() : null;
    });
  }

  function iniciar() {
    if (!TOKEN) {
      ["resumen", "recursos", "hallazgos", "corridas"].forEach(function (t) { $("t-" + t).hidden = true; });
      $("t-sin-token").hidden = false;
      $("sub").textContent = "Sin token";
      return;
    }
    api("/api/orgs").then(function (orgs) {
      estado.orgs = orgs;
      if (!orgs.length) { $("sub").textContent = "No hay corridas con el contrato v2 en la base."; return; }
      var s = $("sel-org");
      orgs.forEach(function (o) { s.appendChild(el("option", { value: o.org_id, text: (o.alias || o.org_id) + " (" + o.n + ")" })); });
      estado.org = orgs[0].org_id;
      return cargarOrg();
    }).catch(function (e) { $("sub").textContent = "Error: " + e.message; });

    $("sel-org").addEventListener("change", function (e) { estado.org = e.target.value; estado.cuenta = ""; cargarOrg().catch(fallo); });
    $("sel-run").addEventListener("change", function (e) { estado.run = e.target.value; cargarRun().catch(fallo); });
    $("sel-cuenta").addEventListener("change", function (e) { estado.cuenta = e.target.value; cargarRun().catch(fallo); });
    try { estado.verUsd = localStorage.getItem("dive-ver-usd") === "1"; } catch (e) { /* sin storage */ }
    $("sel-moneda").addEventListener("change", function (e) {
      estado.verUsd = e.target.value === "USD";
      try { localStorage.setItem("dive-ver-usd", estado.verUsd ? "1" : "0"); } catch (err) { /* sin storage */ }
      pintarMoneda(); pintarResumen(); pintarRecursos(); pintarHallazgos(); pintarCorridas();
    });
    ["r-reg", "r-svc", "r-tipo", "r-hall"].forEach(function (id) {
      $(id).addEventListener("change", function () { estado.pagina = 0; guardarFiltros(); pintarRecursos(); });
    });
    $("r-q").addEventListener("input", function () { estado.pagina = 0; guardarFiltros(); pintarRecursos(); });
    ["f-presencia", "f-estado", "f-sev", "f-cat", "f-verif"].forEach(function (id) {
      $(id).addEventListener("change", function () { guardarFiltros(); pintarHallazgos(); });
    });
    $("f-q").addEventListener("input", function () { guardarFiltros(); pintarHallazgos(); });
    document.querySelectorAll("nav button").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll("nav button").forEach(function (x) { x.setAttribute("aria-selected", String(x === b)); });
        ["resumen", "recursos", "hallazgos", "corridas", "informe"].forEach(function (t) { $("t-" + t).hidden = t !== b.dataset.tab; });
        try { sessionStorage.setItem("dive-pestana", b.dataset.tab); } catch (e) { /* sin storage */ }
        if (b.dataset.tab === "informe") cargarInforme();
      });
    });
    restaurarFiltros();
    try {
      var pest = sessionStorage.getItem("dive-pestana");
      if (pest && ["resumen", "recursos", "hallazgos", "corridas"].indexOf(pest) >= 0) document.querySelector('nav button[data-tab="' + pest + '"]').click();
    } catch (e) { /* sin storage */ }
    var tema = null;
    try { tema = localStorage.getItem("dive-tema"); } catch (e) { /* sin storage */ }
    if (tema) document.documentElement.setAttribute("data-theme", tema);
    $("btn-tema").addEventListener("click", function () {
      var oscuro = document.documentElement.getAttribute("data-theme") === "dark" ||
        (!document.documentElement.getAttribute("data-theme") && window.matchMedia("(prefers-color-scheme: dark)").matches);
      var nuevo = oscuro ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", nuevo);
      try { localStorage.setItem("dive-tema", nuevo); } catch (e) { /* sin storage */ }
    });
  }

  iniciar();
})();
