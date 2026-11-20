"""ClinAgenda - servidor web (sin dependencias externas).

Uso:  python servidor.py y abrir  http://localhost:8000
"""
import json
import os
import re
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ_SERVIDOR = Path(__file__).resolve().parent
RAIZ_PROYECTO = RAIZ_SERVIDOR.parent if (RAIZ_SERVIDOR.parent / "Codigos_Python").exists() or (RAIZ_SERVIDOR.parent / "nucleo.py").exists() else RAIZ_SERVIDOR

for _ruta_mod in (RAIZ_PROYECTO / "Codigos_Python", RAIZ_PROYECTO, RAIZ_SERVIDOR):
    if _ruta_mod.exists() and str(_ruta_mod) not in sys.path:
        sys.path.insert(0, str(_ruta_mod))

import nucleo
from nucleo import ErrorNegocio

RUTA_DB = os.environ.get("MSLS.BD", str(RAIZ_PROYECTO / "MSLS.db"))
MAX_CUERPO = 100_000


def _carpeta_html() -> Path:
    for candidata in (
        RAIZ_PROYECTO / "estructura_html",
        RAIZ_SERVIDOR / "estructura_html",
        RAIZ_PROYECTO / "static",
        RAIZ_SERVIDOR / "static",
        RAIZ_PROYECTO,
    ):
        if (candidata / "index.html").exists():
            return candidata
    return RAIZ_PROYECTO / "estructura_html"


def preparar_base():
    """Crea las tablas y, si la base está vacía, datos INVENTADOS de demostración."""
    con = nucleo.conectar(RUTA_DB)
    if not con.execute("SELECT 1 FROM clinicas").fetchone():
        con.execute("INSERT INTO clinicas (nombre) VALUES ('Clínica Centro'),('Clínica Norte'),('Clínica Sur')")
        con.execute("INSERT INTO medicos (nombre) VALUES ('Dr. Salazar'),('Dra. Mendoza'),('Dr. Vega'),('Dra. Castillo')")
        for nombre, nac in [("Juan Pérez", "1980-05-10"), ("María López", "1975-11-02"),
                            ("Pedro Andrade", "2016-08-21"), ("Carmen Ruiz", "1948-03-14")]:
            nucleo.registrar_paciente(con, "sistema", nombre, nac)
    con.close()


def listar_datos(con):
    f = lambda q, *a: [dict(r) for r in con.execute(q, a)]
    return {
        "clinicas": f("SELECT id, nombre FROM clinicas ORDER BY id"),
        "medicos": f("SELECT id, nombre FROM medicos WHERE activo=1 ORDER BY nombre"),
        "pacientes": f("SELECT id, nombre, fecha_nacimiento, tipo_doc, documento, telefono "
                       "FROM pacientes ORDER BY nombre_clave"),
        "citas": f("SELECT c.id, c.inicio, c.fin, c.estado, p.nombre AS paciente, "
                   "m.nombre AS medico, cl.nombre AS clinica "
                   "FROM citas c JOIN pacientes p ON p.id=c.paciente_id "
                   "JOIN medicos m ON m.id=c.medico_id JOIN clinicas cl ON cl.id=c.clinica_id "
                   "WHERE c.inicio >= date('now','-1 day','localtime') "
                   "ORDER BY c.inicio LIMIT 300"),
    }


def entero(valor, que):
    try:
        return int(valor)
    except (TypeError, ValueError):
        raise ErrorNegocio(f"Falta elegir: {que}.")


class Manejador(BaseHTTPRequestHandler):
    server_version = "ClinAgenda"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _responder(self, codigo, datos, tipo="application/json; charset=utf-8"):
        cuerpo = datos if isinstance(datos, bytes) else json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(cuerpo)

    def do_GET(self):
        ruta = self.path.split("?", 1)[0]
        carpeta_web = _carpeta_html()

        if ruta in ("/", "/index.html"):
            archivo_html = carpeta_web / "index.html"
            if not archivo_html.is_file():
                return self._responder(404, {"error": "No se encontró index.html."})
            self._responder(200, archivo_html.read_bytes(), "text/html; charset=utf-8")
        elif ruta == "/Fondo_oscuro.jpg":
            archivo_fondo = carpeta_web / "Fondo_oscuro.jpg"
            if not archivo_fondo.is_file():
                return self._responder(404, {"error": "No se encontró Fondo_oscuro.jpg."})
            self._responder(200, archivo_fondo.read_bytes(), "image/jpeg")
        elif ruta == "/api/datos":
            self._con(lambda con: listar_datos(con))
        else:
            self._responder(404, {"error": "Página no encontrada."})

    def do_POST(self):
        try:
            largo = int(self.headers.get("Content-Length") or 0)
            if largo > MAX_CUERPO:
                raise ErrorNegocio("Lo enviado es demasiado grande.")
            datos = json.loads(self.rfile.read(largo) or b"{}")
            if not isinstance(datos, dict):
                raise ValueError
        except ErrorNegocio as e:
            return self._responder(400, {"error": str(e)})
        except ValueError:
            return self._responder(400, {"error": "No se entendió lo enviado. Recargue la página e intente de nuevo."})
        usuario = nucleo.normalizar_espacios(str(datos.get("usuario") or "recepcion"))[:50] or "recepcion"
        ruta = self.path

        def accion(con):
            if ruta == "/api/pacientes":
                pid = nucleo.registrar_paciente(
                    con, usuario, datos.get("nombre"), datos.get("fecha_nacimiento"),
                    datos.get("tipo_doc") or "ninguno", datos.get("documento"),
                    datos.get("telefono"), datos.get("correo"), datos.get("contacto_responsable"),
                    confirmar_homonimo=bool(datos.get("confirmar_homonimo")))
                return {"id": pid, "mensaje": "Paciente registrado."}
            if ruta == "/api/citas":
                inicio = f"{datos.get('fecha') or ''} {datos.get('hora') or ''}".strip()
                if not datos.get("fecha") or not datos.get("hora"):
                    raise ErrorNegocio("Falta elegir la fecha y la hora de la cita.")
                cid = nucleo.agendar_cita(
                    con, usuario, entero(datos.get("paciente_id"), "el paciente"),
                    entero(datos.get("medico_id"), "el médico"),
                    entero(datos.get("clinica_id"), "la clínica"), inicio,
                    entero(datos.get("duracion") or 30, "la duración"))
                return {"id": cid, "mensaje": "Cita agendada."}
            m = re.fullmatch(r"/api/citas/(\d+)/(cancelar|deshacer)", ruta)
            if m:
                fn = nucleo.cancelar_cita if m.group(2) == "cancelar" else nucleo.deshacer_cancelacion
                return {"mensaje": fn(con, usuario, int(m.group(1)))}
            raise ErrorNegocio("Acción no encontrada.")

        self._con(accion)

    def _con(self, fn):
        con = None
        try:
            con = nucleo.conectar(RUTA_DB)
            self._responder(200, fn(con))
        except ErrorNegocio as e:
            self._responder(400, {"error": str(e), "codigo": e.codigo})
        except Exception:
            traceback.print_exc()  # el detalle técnico queda en la consola, no en la pantalla
            self._responder(500, {"error": "Algo salió mal de nuestro lado. Sus datos no se perdieron; "
                                           "intente de nuevo en un momento."})
        finally:
            if con:
                con.close()


if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 8000))
    preparar_base()
    print(f"ClinAgenda lista en http://localhost:{puerto}  (Ctrl+C para detener)")
    ThreadingHTTPServer(("127.0.0.1", puerto), Manejador).serve_forever()