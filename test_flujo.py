"""Pruebas del flujo completo con datos simulados (sin red ni Telegram)."""
import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import chollos

AHORA = time.time()
BUEN_VENDEDOR = {"valoraciones": 40, "ventas": 50, "nota": 4.9, "dias": 800, "profesional": False}
SIN_VALORACIONES = {"valoraciones": 0, "ventas": 0, "nota": None, "dias": 5, "profesional": False}


def anuncio(id_, precio=150, titulo="iPhone 13 128GB", km_lat=40.42, horas=1, reservado=False, user="u1"):
    return {"id": id_, "user_id": user, "titulo": titulo, "desc": "Muy bien cuidado", "precio": float(precio),
            "lat": km_lat, "lon": -3.70, "ciudad": "Madrid", "reservado": reservado, "envio": True,
            "url": "https://es.wallapop.com/item/" + id_, "foto": None, "creado": AHORA - horas * 3600}


def vinted_item(id_, precio=130, titulo="iPhone 13 128GB"):
    return {"id": "v" + id_, "titulo": titulo, "desc": "", "precio": float(precio),
            "coste_total": precio + 11.0, "estado": "Bueno", "lat": None, "lon": None, "ciudad": "",
            "reservado": False, "envio": True, "url": "https://www.vinted.es/items/" + id_,
            "foto": None, "creado": 0, "origen": "vinted"}


class Flujo(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        cfg = chollos.leer_json(Path(chollos.__file__).parent / "config.json", {})
        cfg["vinted"] = {"activo": False, "envio_estimado": 4}
        cfg["chollos"] = {"activo": False, "max_horas": 24}
        cfg["cashconverters"] = {"activo": False, "envio": 5, "envio_gratis_desde": 50}
        cfg["presupuesto_max"] = 500
        cfg["ubicacion"] = {"nombre": "Prueba", "lat": 40.4168, "lon": -3.7038, "radio_km": 30}
        self.cfg = cfg
        (self.tmp / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        mercado = {"iPhone 13": {"t": AHORA, "variantes": {"128": {"mediana": 212.0, "n": 10}}}}
        (self.tmp / "mercado.json").write_text(json.dumps(mercado), encoding="utf-8")

        self.enviados, self.fallar_telegram = [], False
        self.wallapop = {"iphone 13": []}
        self.vendedores = {}
        self.parches = [
            mock.patch.object(chollos, "CONFIG", self.tmp / "config.json"),
            mock.patch.object(chollos, "ESTADO", self.tmp / "estado.json"),
            mock.patch.object(chollos, "MERCADO", self.tmp / "mercado.json"),
            mock.patch.object(chollos, "pausa", lambda cfg: None),
            mock.patch("time.sleep", lambda s: None),
            mock.patch.object(chollos, "wallapop_buscar", self._buscar),
            mock.patch.object(chollos, "wallapop_vendedor", lambda uid, cfg: self.vendedores.get(uid, BUEN_VENDEDOR)),
            mock.patch.object(chollos, "enviar_aviso", self._enviar),
            mock.patch.object(chollos, "enviar_texto", lambda t: self.enviados.append(("texto", t))),
        ]
        for p in self.parches:
            p.start()

    def tearDown(self):
        for p in self.parches:
            p.stop()

    def _buscar(self, texto, cfg, local=True, token=None):
        if self.wallapop is None:
            raise RuntimeError("sin red")
        return list(self.wallapop.get(texto, [])), None

    def _enviar(self, it, texto):
        if self.fallar_telegram:
            raise RuntimeError("Telegram caído")
        self.enviados.append((it["id"], texto))

    def ronda(self, **kw):
        a = argparse.Namespace(prueba=kw.get("prueba", False), presupuesto=None, margen=None, horas=None)
        chollos.ejecutar(a)

    def ids(self):
        return [i for i, _ in self.enviados if i != "texto"]

    # ---- Wallapop
    def test_chollo_se_avisa_una_sola_vez(self):
        self.wallapop["iphone 13"] = [anuncio("a1")]
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])
        self.ronda()
        self.assertEqual(self.ids(), ["a1"], "no debe repetir el aviso")

    def test_aviso_dice_beneficio_y_vendedor(self):
        self.wallapop["iphone 13"] = [anuncio("a1")]
        self.ronda()
        texto = self.enviados[0][1]
        self.assertIn("Beneficio estimado", texto)
        self.assertIn("40 valoraciones", texto)

    def test_vendedor_sin_valoraciones_se_descarta(self):
        self.wallapop["iphone 13"] = [anuncio("a1", user="malo")]
        self.vendedores["malo"] = SIN_VALORACIONES
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_vendedor_con_pocas_valoraciones_avisa_pero_llega(self):
        self.wallapop["iphone 13"] = [anuncio("a1", user="nuevo")]
        self.vendedores["nuevo"] = dict(BUEN_VENDEDOR, valoraciones=4)
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])
        self.assertIn("4 valoraciones", self.enviados[0][1])

    def test_vendedor_con_muy_pocas_valoraciones_se_descarta(self):
        self.wallapop["iphone 13"] = [anuncio("a1", user="nuevo")]
        self.vendedores["nuevo"] = dict(BUEN_VENDEDOR, valoraciones=2)
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_precio_demasiado_bajo_para_ser_real_se_descarta(self):
        self.wallapop["iphone 13"] = [anuncio("a1", precio=135)]  # mercado 212: 135 > 45%, llega
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])
        self.wallapop["iphone 13"] = [anuncio("a2", precio=90)]   # 90 < 45% de 212: estafa probable
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])

    def test_vendedor_ilegible_llega_con_aviso(self):
        self.wallapop["iphone 13"] = [anuncio("a1", user="x")]
        self.vendedores["x"] = None
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])
        self.assertIn("No pude comprobar al vendedor", self.enviados[0][1])

    def test_si_telegram_falla_se_reintenta_despues(self):
        self.wallapop["iphone 13"] = [anuncio("a1")]
        self.fallar_telegram = True
        self.ronda()
        self.assertEqual(self.ids(), [])
        self.fallar_telegram = False
        self.ronda()
        self.assertEqual(self.ids(), ["a1"], "debe llegar en la siguiente ronda")

    def test_no_avisa_sin_margen_suficiente(self):
        self.wallapop["iphone 13"] = [anuncio("a1", precio=200)]  # 212*0.92=195 < 200
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_filtros_de_distancia_edad_reserva_y_presupuesto(self):
        self.wallapop["iphone 13"] = [
            anuncio("lejos", km_lat=41.5),            # ~120 km
            anuncio("viejo", horas=30),
            anuncio("reservado", reservado=True),
            anuncio("caro", precio=600),
            anuncio("roto", titulo="iPhone 13 128GB para piezas"),
            anuncio("pro", titulo="iPhone 13 Pro 128GB"),
            anuncio("sincap", titulo="iPhone 13 azul"),
        ]
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_bajada_de_precio_se_reevalua(self):
        self.wallapop["iphone 13"] = [anuncio("a1", precio=200)]
        self.ronda()
        self.assertEqual(self.ids(), [])
        self.wallapop["iphone 13"] = [anuncio("a1", precio=150)]
        self.ronda()
        self.assertEqual(self.ids(), ["a1"])

    def test_orden_de_avisos_por_beneficio(self):
        self.wallapop["iphone 13"] = [anuncio("medio", precio=160), anuncio("mejor", precio=135)]
        self.ronda()
        self.assertEqual(self.ids(), ["mejor", "medio"])

    def test_modo_prueba_no_envia_ni_guarda(self):
        self.wallapop["iphone 13"] = [anuncio("a1")]
        self.ronda(prueba=True)
        self.assertEqual(self.ids(), [])
        self.assertFalse((self.tmp / "estado.json").exists())

    def test_wallapop_caido_avisa_una_vez(self):
        self.wallapop = None
        self.ronda()
        self.ronda()
        avisos = [t for i, t in self.enviados if i == "texto"]
        self.assertEqual(len(avisos), 1, "el aviso de fallo no debe repetirse en 12 h")

    def test_latido_avisa_si_el_bot_esta_parado(self):
        (self.tmp / "estado.json").write_text(json.dumps({"ultima_ronda": AHORA - 5 * 3600}), encoding="utf-8")
        chollos.latido()
        self.assertIn("parece parado", self.enviados[-1][1])

    def test_latido_normal_cuenta_rondas_y_avisos(self):
        self.wallapop["iphone 13"] = [anuncio("a1")]
        self.ronda()
        self.enviados.clear()
        chollos.latido()
        self.assertIn("1 rondas", self.enviados[-1][1])
        self.assertIn("1 avisos", self.enviados[-1][1])

    # ---- Vinted
    def activar_vinted(self, items, vendedor=BUEN_VENDEDOR, desc="Como nuevo"):
        self.cfg["vinted"]["activo"] = True
        (self.tmp / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        self.vinted = items
        for p in [
            mock.patch.object(chollos, "vinted_abrir", lambda: object()),
            mock.patch.object(chollos, "vinted_buscar",
                              lambda op, texto, cfg: list(self.vinted) if texto == "iphone 13" else []),
            mock.patch.object(chollos, "vinted_ficha", lambda op, url: (desc, vendedor)),
        ]:
            p.start()
            self.parches.append(p)

    def test_vinted_primera_vez_solo_apunta(self):
        self.activar_vinted([vinted_item("1")])
        self.ronda()
        self.assertEqual(self.ids(), [], "lo ya existente no se avisa")
        self.vinted.append(vinted_item("2", precio=140))
        self.ronda()
        self.assertEqual(self.ids(), ["v2"], "lo nuevo sí")

    def test_vinted_vendedor_sin_valoraciones_se_descarta(self):
        self.activar_vinted([], vendedor=SIN_VALORACIONES)
        self.ronda()
        self.vinted.append(vinted_item("2", precio=140))
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_vinted_descripcion_con_defecto_se_descarta(self):
        self.activar_vinted([], desc="Funciona todo pero el Face ID no funciona")
        self.ronda()
        self.vinted.append(vinted_item("2", precio=140))
        self.ronda()
        self.assertEqual(self.ids(), [])

    def test_vinted_cuenta_proteccion_y_envio(self):
        self.activar_vinted([])
        self.ronda()
        self.vinted.append(vinted_item("2", precio=185))  # 185+11=196 > 195 de venta: sin margen
        self.ronda()
        self.assertEqual(self.ids(), [])

    # ---- Cash Converters
    def test_cash_converters_primera_vez_apunta_y_luego_avisa(self):
        self.cfg["cashconverters"] = {"activo": True, "envio": 5, "envio_gratis_desde": 50}
        (self.tmp / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

        def art(pid, precio):
            return {"id": "k" + pid, "titulo": "apple iphone 13 128gb", "desc": "", "precio": float(precio),
                    "coste_total": float(precio), "estado": "Bueno", "lat": None, "lon": None, "ciudad": "",
                    "reservado": False, "envio": True, "url": "https://www.cashconverters.es/" + pid,
                    "foto": None, "creado": 0, "origen": "cashconverters"}
        self.cc = [art("A", 140)]
        with mock.patch.object(chollos, "cc_buscar", lambda texto, cfg: list(self.cc) if texto == "iphone 13" else []):
            self.ronda()
            self.assertEqual(self.ids(), [], "lo ya existente no se avisa")
            self.cc.append(art("B", 150))
            self.ronda()
            self.ronda()
        self.assertEqual(self.ids(), ["kB"], "lo nuevo avisa una sola vez")
        self.assertIn("Cash Converters", self.enviados[0][1])

    def test_cash_converters_mismo_articulo_en_varias_tiendas_avisa_una_vez(self):
        self.cfg["cashconverters"] = {"activo": True, "envio": 5, "envio_gratis_desde": 50}
        (self.tmp / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")

        def art(pid):
            return {"id": "k" + pid, "titulo": "apple iphone 13 128gb", "desc": "", "precio": 150.0,
                    "coste_total": 150.0, "estado": "Bueno", "lat": None, "lon": None, "ciudad": "",
                    "reservado": False, "envio": True, "url": "https://www.cashconverters.es/" + pid,
                    "foto": None, "creado": 0, "origen": "cashconverters"}
        self.cc = []
        with mock.patch.object(chollos, "cc_buscar", lambda texto, cfg: list(self.cc) if texto == "iphone 13" else []):
            self.ronda()  # primera vez: base vacía
            self.cc = [art("A"), art("B"), art("C")]
            self.ronda()
            self.cc = [art("A"), art("B"), art("C"), art("D")]  # otra tienda lo añade después
            self.ronda()
        self.assertEqual(len(self.ids()), 1, "un solo aviso para el mismo artículo")
        self.assertIn("3 unidades", self.enviados[0][1])

    # ---- Chollos online
    def test_chollo_de_aliexpress_se_avisa(self):
        self.cfg["chollos"]["activo"] = True
        (self.tmp / "config.json").write_text(json.dumps(self.cfg), encoding="utf-8")
        oferta = {"id": "chttps://x/o/1", "titulo": "Apple iPhone 13 128GB", "desc": "", "precio": 140.0,
                  "tienda": "AliExpress", "url": "https://x/o/1", "foto": None, "creado": AHORA - 3600,
                  "origen": "chollo", "lat": None, "lon": None, "ciudad": "", "reservado": False, "envio": True}
        with mock.patch.object(chollos, "leer_chollometro", lambda: [oferta]):
            self.ronda()
            self.ronda()
        self.assertEqual(self.ids(), ["chttps://x/o/1"])
        self.assertIn("AliExpress", self.enviados[0][1])
        self.assertIn("semanas", self.enviados[0][1])


if __name__ == "__main__":
    unittest.main()
