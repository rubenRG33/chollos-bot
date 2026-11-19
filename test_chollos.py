import os
import unittest
from unittest import mock

import chollos

CFG = chollos.leer_json(chollos.CONFIG, {})
PROD = {p["nombre"]: p for p in CFG["productos"]}


def anuncio(titulo, precio=300, desc="", reservado=False):
    return {"id": titulo, "titulo": titulo, "desc": desc, "precio": float(precio),
            "reservado": reservado}


class Clasificacion(unittest.TestCase):
    def test_variante_iphone(self):
        self.assertEqual(chollos.clasificar(PROD["iPhone 13"], anuncio("iPhone 13 128 GB azul"), CFG), "128")

    def test_iphone_pro_no_es_iphone_normal(self):
        self.assertIsNone(chollos.clasificar(PROD["iPhone 13"], anuncio("iPhone 13 Pro 256gb"), CFG))

    def test_iphone_sin_capacidad_se_descarta(self):
        self.assertIsNone(chollos.clasificar(PROD["iPhone 13"], anuncio("iPhone 13 como nuevo"), CFG))

    def test_averiado_en_descripcion(self):
        a = anuncio("iPhone 12 64gb", desc="Face ID no funciona")
        self.assertIsNone(chollos.clasificar(PROD["iPhone 12"], a, CFG))

    def test_idiomas_extranjeros_y_mandos_se_descartan(self):
        self.assertIsNone(chollos.clasificar(PROD["PS5"], anuncio("CERCO Playstation 5 slim"), CFG))
        self.assertIsNone(chollos.clasificar(PROD["PS5"], anuncio("Manette PS5 GTA"), CFG))
        self.assertIsNone(chollos.clasificar(PROD["iPhone 13"], anuncio("iPhone 13 128GB Mezzanotte"), CFG))

    def test_videojuego_por_categoria_no_es_consola(self):
        juego = anuncio("Animal Well (Nintendo Switch)", 75)
        juego["categoria"] = "Gaming: consolas y videojuegos > Videojuegos y más > Videojuegos"
        self.assertIsNone(chollos.clasificar(PROD["Nintendo Switch"], juego, CFG))
        consola = anuncio("Nintendo Switch OLED", 150)
        consola["categoria"] = "Gaming: consolas y videojuegos > Consolas y accesorios > Consolas"
        self.assertEqual(chollos.clasificar(PROD["Nintendo Switch"], consola, CFG), "oled")

    def test_accesorio_por_categoria_se_descarta(self):
        a = anuncio("Nintendo Switch Joy-Con Pair", 80)
        a["categoria"] = "Gaming > Consolas y accesorios > Accesorios y recambios de consolas"
        self.assertIsNone(chollos.clasificar(PROD["Nintendo Switch"], a, CFG))

    def test_busco_se_descarta(self):
        self.assertIsNone(chollos.clasificar(PROD["iPhone 12"], anuncio("Busco iPhone 12 64gb"), CFG))

    def test_ps5_digital_y_disco(self):
        self.assertEqual(chollos.clasificar(PROD["PS5"], anuncio("PS5 digital"), CFG), "digital")
        self.assertEqual(chollos.clasificar(PROD["PS5"], anuncio("PlayStation 5 con lector"), CFG), "disco")

    def test_ps5_mando_no_es_consola(self):
        self.assertIsNone(chollos.clasificar(PROD["PS5"], anuncio("Mando PS5 DualSense"), CFG))

    def test_switch_oled(self):
        self.assertEqual(chollos.clasificar(PROD["Nintendo Switch"], anuncio("Nintendo Switch OLED"), CFG), "oled")

    def test_switch_juego_no_es_consola(self):
        self.assertIsNone(chollos.clasificar(PROD["Nintendo Switch"], anuncio("Juego Nintendo Switch Zelda"), CFG))

    def test_apple_watch_serie(self):
        self.assertEqual(chollos.clasificar(PROD["Apple Watch"], anuncio("Apple Watch Series 7 45mm"), CFG), "7")
        self.assertEqual(chollos.clasificar(PROD["Apple Watch"], anuncio("Apple Watch SE 40mm"), CFG), "se")

    def test_apple_watch_correa_no(self):
        self.assertIsNone(chollos.clasificar(PROD["Apple Watch"], anuncio("Correa Apple Watch 7"), CFG))

    def test_mancuerna_suelta_no_es_un_par(self):
        self.assertIsNone(chollos.clasificar(PROD["Mancuernas ajustables"], anuncio("Mancuerna ajustable 24kg", 90), CFG))
        self.assertEqual(chollos.clasificar(PROD["Mancuernas ajustables"], anuncio("Mancuernas ajustables 24kg", 90), CFG), "24")

    def test_kettlebell_kg(self):
        self.assertEqual(chollos.clasificar(PROD["Kettlebell"], anuncio("Kettlebell 16 kg"), CFG), "16")


class Mercado(unittest.TestCase):
    def test_mediana_por_variante_y_sin_raros(self):
        items = [anuncio("iPhone 13 128gb", p) for p in (300, 310, 320, 330, 20, 900)]
        items += [anuncio("iPhone 13 256gb", p) for p in (400, 410)]
        m = chollos.calcular_mercado(items, PROD["iPhone 13"], CFG)
        self.assertEqual(m["128"]["n"], 4)
        self.assertEqual(m["128"]["mediana"], 315)
        self.assertEqual(m["256"]["n"], 2)

    def test_ignora_reservados(self):
        items = [anuncio("iPhone 13 128gb", 300, reservado=True)]
        self.assertEqual(chollos.calcular_mercado(items, PROD["iPhone 13"], CFG), {})


class Beneficio(unittest.TestCase):
    def test_beneficio_con_descuento_de_venta(self):
        ev = chollos.evaluar({"precio": 100.0}, {"mediana": 200.0, "n": 10}, CFG)
        self.assertEqual(ev["venta"], 184)
        self.assertEqual(ev["beneficio"], 84)
        self.assertFalse(ev["sospechoso"])

    def test_precio_demasiado_bajo_es_sospechoso(self):
        ev = chollos.evaluar({"precio": 50.0}, {"mediana": 300.0, "n": 10}, CFG)
        self.assertTrue(ev["sospechoso"])


class Distancia(unittest.TestCase):
    def test_sol_y_getafe_estan_a_unos_12km(self):
        d = chollos.distancia_km(40.4168, -3.7038, 40.3057, -3.7327)
        self.assertTrue(11 < d < 14, d)


class Ubicacion(unittest.TestCase):
    def test_la_ubicacion_viene_de_variables(self):
        cfg = {"ubicacion": {"lat": 0, "lon": 0, "radio_km": 30}}
        with mock.patch.dict(os.environ, {"CHOLLOS_LAT": "40.5", "CHOLLOS_LON": "-3.7", "CHOLLOS_RADIO": "20"}):
            chollos.aplicar_entorno(cfg)
        self.assertEqual(cfg["ubicacion"], {"lat": 40.5, "lon": -3.7, "radio_km": 20.0})

    def test_sin_ubicacion_la_config_no_es_valida(self):
        cfg = chollos.leer_json(chollos.CONFIG, {})
        cfg["ubicacion"] = {"lat": 0, "lon": 0, "radio_km": 30}
        errores = chollos.validar_config(cfg)
        self.assertTrue(any("ubicación" in e for e in errores), errores)

    def test_config_del_repositorio_no_lleva_ubicacion_real(self):
        cfg = chollos.leer_json(chollos.CONFIG, {})
        self.assertEqual((cfg["ubicacion"]["lat"], cfg["ubicacion"]["lon"]), (0, 0))


class Vinted(unittest.TestCase):
    PAG = ('<img src="https://x/f.webp?s=1" alt="a" class="c" data-testid="product-item-id-111--image--img"/>'
           '<a href="/items/111-iphone-13?referrer=catalog" class="k" data-testid="product-item-id-111--overlay-link" '
           'title="iPhone 13, 128 GB, Marca: Apple, Modelo: iPhone 13, Estado: Muy bueno, 300.00 €, 315.70 €" target="_self"></a>'
           '<a href="/items/222-funda?referrer=catalog" data-testid="product-item-id-222--overlay-link" '
           'title="Funda, 5.00 €" ></a>')

    def test_parsea_precios_estado_y_foto(self):
        its = chollos.parsear_vinted(self.PAG, CFG)
        self.assertEqual(len(its), 2)
        a = its[0]
        self.assertEqual((a["id"], a["titulo"], a["precio"], a["estado"]), ("v111", "iPhone 13, 128 GB", 300.0, "Muy bueno"))
        self.assertEqual(a["coste_total"], 315.7 + CFG["vinted"]["envio_estimado"])
        self.assertEqual(a["foto"], "https://x/f.webp?s=1")
        self.assertEqual(a["url"], "https://www.vinted.es/items/111-iphone-13")

    def test_titulo_sin_marca_ni_estado(self):
        a = chollos.parsear_vinted(self.PAG, CFG)[1]
        self.assertEqual((a["titulo"], a["precio"], a["estado"]), ("Funda", 5.0, ""))

    def test_beneficio_usa_coste_con_proteccion_y_envio(self):
        ev = chollos.evaluar({"precio": 100.0, "coste_total": 120.0}, {"mediana": 200.0, "n": 10}, CFG)
        self.assertEqual(ev["beneficio"], 64)


class Vendedor(unittest.TestCase):
    def v(self, n, pro=False):
        return {"valoraciones": n, "profesional": pro}

    def test_sin_valoraciones_se_descarta(self):
        self.assertEqual(chollos.juzgar_vendedor(self.v(0), CFG), "descartar")

    def test_pocas_valoraciones_avisa(self):
        self.assertEqual(chollos.juzgar_vendedor(self.v(3), CFG), "aviso")

    def test_vendedor_con_historial_ok(self):
        self.assertEqual(chollos.juzgar_vendedor(self.v(40), CFG), "ok")

    def test_tienda_profesional_sin_valoraciones_ok(self):
        self.assertEqual(chollos.juzgar_vendedor(self.v(0, pro=True), CFG), "ok")

    def test_vendedor_desconocido_avisa(self):
        self.assertEqual(chollos.juzgar_vendedor(None, CFG), "aviso")


class Chollometro(unittest.TestCase):
    XML = ('<?xml version="1.0"?><rss xmlns:pepper="http://www.pepper.com/rss" '
           'xmlns:media="http://search.yahoo.com/mrss/" version="2.0"><channel>'
           '<item><title>181° - iPhone 13 128GB</title><link>https://x/o/1</link>'
           '<pepper:merchant name="AliExpress" price="1.099,50€"/>'
           '<media:thumbnail url="https://x/i.jpg"/><pubDate>Thu, 19 Nov 2026 12:00:46 +0100</pubDate></item>'
           '<item><title>Sin precio</title><link>https://x/o/2</link></item></channel></rss>')

    def test_numeros_en_formato_espanol(self):
        self.assertEqual(chollos.numero_es("326,45€"), 326.45)
        self.assertEqual(chollos.numero_es("1.099,50€"), 1099.5)
        self.assertEqual(chollos.numero_es("1.299€"), 1299.0)
        self.assertEqual(chollos.numero_es("49 €"), 49.0)
        self.assertIsNone(chollos.numero_es(""))

    def test_parsea_oferta_y_descarta_sin_precio(self):
        its = chollos.parsear_chollometro(self.XML)
        self.assertEqual(len(its), 1)
        o = its[0]
        self.assertEqual((o["titulo"], o["precio"], o["tienda"]), ("iPhone 13 128GB", 1099.5, "AliExpress"))
        self.assertEqual(o["foto"], "https://x/i.jpg")
        self.assertGreater(o["creado"], 0)


if __name__ == "__main__":
    unittest.main()
