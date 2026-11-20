"""ClinAgenda - núcleo (parte 1): datos limpios y citas que no se cruzan.

Solo usa la biblioteca estándar de Python (sqlite3), sin instalar nada.
Las horas se guardan como hora local sin zona horaria (Ecuador no tiene
cambio de horario). Ver DECISIONES.md.
"""
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, date


class ErrorNegocio(Exception):
    """Error pensado para mostrarse tal cual a una persona (sin jerga técnica).
    `codigo` permite a la interfaz ofrecer una acción (ej. 'homonimo')."""

    def __init__(self, mensaje, codigo=None):
        super().__init__(mensaje)
        self.codigo = codigo


# ----------------------------------------------------------------- Base de datos
ESQUEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS clinicas (
    id INTEGER PRIMARY KEY, nombre TEXT NOT NULL UNIQUE);

CREATE TABLE IF NOT EXISTS medicos (
    id INTEGER PRIMARY KEY, nombre TEXT NOT NULL, activo INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS pacientes (
    id INTEGER PRIMARY KEY,
    nombre TEXT NOT NULL,            -- como se muestra: "María López"
    nombre_clave TEXT NOT NULL,      -- para comparar: "maria lopez"
    fecha_nacimiento TEXT NOT NULL,
    tipo_doc TEXT NOT NULL CHECK (tipo_doc IN ('cedula','pasaporte','ninguno')),
    documento TEXT,
    telefono TEXT,
    correo TEXT,
    contacto_responsable TEXT,       -- tutor (niños) o familiar, si no hay teléfono propio
    creado_en TEXT NOT NULL);

-- Un mismo documento no puede pertenecer a dos pacientes.
CREATE UNIQUE INDEX IF NOT EXISTS ux_paciente_doc
    ON pacientes (tipo_doc, documento) WHERE documento IS NOT NULL;

CREATE TABLE IF NOT EXISTS citas (
    id INTEGER PRIMARY KEY,
    paciente_id INTEGER NOT NULL REFERENCES pacientes(id),
    medico_id INTEGER NOT NULL REFERENCES medicos(id),
    clinica_id INTEGER NOT NULL REFERENCES clinicas(id),
    inicio TEXT NOT NULL,
    fin TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'activa' CHECK (estado IN ('activa','cancelada')),
    creado_por TEXT NOT NULL,
    creado_en TEXT NOT NULL);

-- Red de seguridad en la base de datos: aunque falle el código, el mismo
-- médico no puede tener dos citas ACTIVAS a la misma hora. Al cancelar, la
-- hora queda libre porque la cita deja de ser 'activa'.
CREATE UNIQUE INDEX IF NOT EXISTS ux_medico_hora_activa
    ON citas (medico_id, inicio) WHERE estado = 'activa';

-- Quién hizo qué y cuándo. Nunca se borra.
CREATE TABLE IF NOT EXISTS auditoria (
    id INTEGER PRIMARY KEY,
    cuando TEXT NOT NULL, usuario TEXT NOT NULL, accion TEXT NOT NULL,
    entidad TEXT NOT NULL, entidad_id INTEGER, detalle TEXT);
"""


def conectar(ruta=":memory:"):
    con = sqlite3.connect(ruta, isolation_level=None)  # transacciones manuales
    con.row_factory = sqlite3.Row
    con.executescript(ESQUEMA)
    return con


@contextmanager
def transaccion(con):
    """BEGIN IMMEDIATE bloquea a otros escritores: dos recepcionistas en el
    mismo segundo se atienden una tras otra, no a la vez."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _auditar(con, usuario, accion, entidad, entidad_id, detalle=""):
    con.execute(
        "INSERT INTO auditoria (cuando, usuario, accion, entidad, entidad_id, detalle)"
        " VALUES (?,?,?,?,?,?)",
        (datetime.now().isoformat(timespec="seconds"), usuario, accion,
         entidad, entidad_id, detalle))


# ----------------------------------------------------------------- Validaciones
def normalizar_espacios(texto):
    return re.sub(r"\s+", " ", (texto or "")).strip()


def _sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def clave_nombre(nombre):
    """'  MARÍA   lópez ' y 'Maria Lopez' dan la misma clave: 'maria lopez'."""
    return _sin_tildes(normalizar_espacios(nombre)).casefold()


def limpiar_nombre(nombre):
    """Valida y deja el nombre presentable. Rechaza emojis, símbolos y código."""
    nombre = normalizar_espacios(nombre)
    if len(nombre) < 3:
        raise ErrorNegocio("Escriba el nombre completo del paciente (nombre y apellido).")
    if len(nombre) > 100:
        raise ErrorNegocio("El nombre es demasiado largo. Revise que no se haya pegado texto de más.")
    if not re.fullmatch(r"[^\W\d_]+(?:[ '\-.][^\W\d_]+)*\.?", nombre):
        raise ErrorNegocio("El nombre solo puede tener letras y espacios. "
                           "Quite números, símbolos o emojis.")
    return nombre.title()


def validar_cedula(cedula):
    """Cédula ecuatoriana: 10 dígitos, provincia 01-24 o 30, tercer dígito < 6
    y dígito verificador correcto (módulo 10)."""
    c = (cedula or "").strip()
    if not c.isdigit():
        raise ErrorNegocio("La cédula solo debe tener números, sin letras ni espacios.")
    if len(c) != 10:
        raise ErrorNegocio(f"La cédula debe tener 10 dígitos (escribió {len(c)}).")
    provincia = int(c[:2])
    if not (1 <= provincia <= 24 or provincia == 30) or int(c[2]) >= 6 or len(set(c)) == 1:
        raise ErrorNegocio("Esa cédula no parece válida. Revise los primeros dígitos.")
    total = 0
    for d, coef in zip(c[:9], [2, 1, 2, 1, 2, 1, 2, 1, 2]):
        p = int(d) * coef
        total += p - 9 if p > 9 else p
    verificador = (10 - total % 10) % 10
    if verificador != int(c[9]):
        raise ErrorNegocio("Esa cédula no es válida: el último dígito no coincide. "
                           "Revise que esté bien escrita.")
    return c


def validar_documento(tipo_doc, documento):
    """Devuelve (tipo, documento) listos para guardar."""
    if tipo_doc == "ninguno":
        return "ninguno", None
    documento = (documento or "").strip()
    if tipo_doc == "cedula":
        return "cedula", validar_cedula(documento)
    if tipo_doc == "pasaporte":
        documento = documento.upper()
        if not re.fullmatch(r"[A-Z0-9]{5,15}", documento):
            raise ErrorNegocio("El pasaporte debe tener entre 5 y 15 letras o números, sin espacios.")
        return "pasaporte", documento
    raise ErrorNegocio("Elija si el paciente tiene cédula, pasaporte o ningún documento.")


def validar_telefono(telefono):
    """Opcional. Celular 09XXXXXXXX o fijo 0[2-7]XXXXXXX (Ecuador)."""
    if not telefono or not telefono.strip():
        return None
    t = re.sub(r"[\s\-()]", "", telefono)
    if t.startswith("+593"):
        t = "0" + t[4:]
    if re.fullmatch(r"09\d{8}", t) or re.fullmatch(r"0[2-7]\d{7}", t):
        return t
    raise ErrorNegocio("El teléfono no parece correcto. Un celular tiene 10 dígitos "
                       "y empieza con 09 (ejemplo: 0991234567).")


def validar_correo(correo):
    """Opcional."""
    if not correo or not correo.strip():
        return None
    c = correo.strip().lower()
    if not re.fullmatch(r"[a-z0-9._%+\-]+@[a-z0-9\-]+(\.[a-z0-9\-]+)*\.[a-z]{2,}", c):
        raise ErrorNegocio("El correo no parece correcto. Debe verse así: nombre@correo.com")
    return c


def validar_fecha_nacimiento(texto, hoy=None):
    hoy = hoy or date.today()
    try:
        f = date.fromisoformat((texto or "").strip())
    except ValueError:
        raise ErrorNegocio("La fecha de nacimiento no es válida. Use el formato año-mes-día "
                           "(ejemplo: 1985-03-27).")
    if f > hoy:
        raise ErrorNegocio("La fecha de nacimiento es futura. Revise el año.")
    if (hoy - f).days > 120 * 365:
        raise ErrorNegocio("La fecha de nacimiento indica más de 120 años. Revise el año.")
    return f.isoformat()


# ----------------------------------------------------------------- Pacientes
def registrar_paciente(con, usuario, nombre, fecha_nacimiento, tipo_doc="ninguno",
                       documento=None, telefono=None, correo=None,
                       contacto_responsable=None, confirmar_homonimo=False, hoy=None):
    """Valida todo y guarda. Si ya existe alguien con el mismo nombre y fecha de
    nacimiento, pide confirmación en vez de crear un duplicado en silencio."""
    nombre_ok = limpiar_nombre(nombre)
    fecha_ok = validar_fecha_nacimiento(fecha_nacimiento, hoy)
    tipo_ok, doc_ok = validar_documento(tipo_doc, documento)
    tel_ok = validar_telefono(telefono)
    correo_ok = validar_correo(correo)
    contacto_ok = validar_telefono(contacto_responsable)
    if tel_ok is None and correo_ok is None and contacto_ok is None:
        # No se bloquea: hay pacientes sin ningún medio de contacto. Se acepta y se avisa.
        pass

    with transaccion(con):
        if doc_ok:
            otro = con.execute("SELECT id, nombre FROM pacientes WHERE tipo_doc=? AND documento=?",
                               (tipo_ok, doc_ok)).fetchone()
            if otro:
                raise ErrorNegocio(f"Ya existe un paciente con ese documento: {otro['nombre']}.")
        if not confirmar_homonimo:
            hom = con.execute("SELECT id FROM pacientes WHERE nombre_clave=? AND fecha_nacimiento=?",
                              (clave_nombre(nombre_ok), fecha_ok)).fetchone()
            if hom:
                raise ErrorNegocio("Ya hay un paciente con el mismo nombre y fecha de nacimiento. "
                                   "Si es otra persona, confirme para crearlo igual.",
                                   codigo="homonimo")
        cur = con.execute(
            "INSERT INTO pacientes (nombre, nombre_clave, fecha_nacimiento, tipo_doc, documento,"
            " telefono, correo, contacto_responsable, creado_en) VALUES (?,?,?,?,?,?,?,?,?)",
            (nombre_ok, clave_nombre(nombre_ok), fecha_ok, tipo_ok, doc_ok, tel_ok, correo_ok,
             contacto_ok, datetime.now().isoformat(timespec="seconds")))
        _auditar(con, usuario, "crear", "paciente", cur.lastrowid)
        return cur.lastrowid


# ----------------------------------------------------------------- Citas
def _fecha_hora(texto):
    try:
        return datetime.fromisoformat((texto or "").strip())
    except ValueError:
        raise ErrorNegocio("La fecha y hora no son válidas. Use el formato 2026-11-23 09:30.")


def agendar_cita(con, usuario, paciente_id, medico_id, clinica_id, inicio,
                 duracion_min=30, ahora=None):
    ahora = ahora or datetime.now()
    ini = _fecha_hora(inicio)
    if ini <= ahora:
        raise ErrorNegocio("Esa fecha y hora ya pasaron. Elija un horario futuro.")
    if not 5 <= duracion_min <= 240:
        raise ErrorNegocio("La duración de la cita debe estar entre 5 minutos y 4 horas.")
    fin = ini + timedelta(minutes=duracion_min)
    ini_s, fin_s = ini.isoformat(timespec="minutes"), fin.isoformat(timespec="minutes")

    try:
        with transaccion(con):
            for tabla, id_, que in (("pacientes", paciente_id, "paciente"),
                                    ("medicos", medico_id, "médico"),
                                    ("clinicas", clinica_id, "clínica")):
                if not con.execute(f"SELECT 1 FROM {tabla} WHERE id=?", (id_,)).fetchone():
                    raise ErrorNegocio(f"No se encontró el {que} seleccionado.")

            # El médico no puede estar en dos sitios a la vez (en ninguna clínica).
            choque = con.execute(
                "SELECT cl.nombre FROM citas c JOIN clinicas cl ON cl.id = c.clinica_id"
                " WHERE c.medico_id=? AND c.estado='activa' AND c.inicio < ? AND c.fin > ?",
                (medico_id, fin_s, ini_s)).fetchone()
            if choque:
                raise ErrorNegocio(f"El médico ya tiene una cita a esa hora en {choque['nombre']}. "
                                   "Elija otro horario.")
            # El paciente tampoco puede tener dos citas a la vez.
            if con.execute(
                    "SELECT 1 FROM citas WHERE paciente_id=? AND estado='activa'"
                    " AND inicio < ? AND fin > ?", (paciente_id, fin_s, ini_s)).fetchone():
                raise ErrorNegocio("Este paciente ya tiene otra cita a esa hora.")

            cur = con.execute(
                "INSERT INTO citas (paciente_id, medico_id, clinica_id, inicio, fin, creado_por, creado_en)"
                " VALUES (?,?,?,?,?,?,?)",
                (paciente_id, medico_id, clinica_id, ini_s, fin_s, usuario,
                 datetime.now().isoformat(timespec="seconds")))
            _auditar(con, usuario, "agendar", "cita", cur.lastrowid, f"{ini_s} medico={medico_id}")
            return cur.lastrowid
    except sqlite3.IntegrityError:
        # Red de seguridad: la base de datos rechazó una doble reserva.
        raise ErrorNegocio("Esa hora acaba de ser tomada por otra persona. Elija otro horario.")


def cancelar_cita(con, usuario, cita_id):
    """Idempotente: cancelar dos veces no da error ni ensucia la auditoría."""
    with transaccion(con):
        cita = con.execute("SELECT estado FROM citas WHERE id=?", (cita_id,)).fetchone()
        if not cita:
            raise ErrorNegocio("No se encontró esa cita.")
        if cita["estado"] == "cancelada":
            return "Esa cita ya estaba cancelada."
        con.execute("UPDATE citas SET estado='cancelada' WHERE id=?", (cita_id,))
        _auditar(con, usuario, "cancelar", "cita", cita_id)
        return "Cita cancelada. La hora quedó libre."


def deshacer_cancelacion(con, usuario, cita_id, ahora=None):
    """Botón 'Deshacer': reactiva la cita solo si su hora sigue libre."""
    ahora = ahora or datetime.now()
    try:
        with transaccion(con):
            c = con.execute("SELECT * FROM citas WHERE id=?", (cita_id,)).fetchone()
            if not c:
                raise ErrorNegocio("No se encontró esa cita.")
            if c["estado"] == "activa":
                return "Esa cita ya estaba activa."
            if datetime.fromisoformat(c["inicio"]) <= ahora:
                raise ErrorNegocio("Esa cita ya pasó, no se puede reactivar.")
            if con.execute("SELECT 1 FROM citas WHERE medico_id=? AND estado='activa'"
                           " AND inicio < ? AND fin > ?", (c["medico_id"], c["fin"], c["inicio"])).fetchone():
                raise ErrorNegocio("No se puede deshacer: esa hora ya fue ocupada por otra cita.")
            con.execute("UPDATE citas SET estado='activa' WHERE id=?", (cita_id,))
            _auditar(con, usuario, "reactivar", "cita", cita_id)
            return "Cita reactivada."
    except sqlite3.IntegrityError:
        raise ErrorNegocio("No se puede deshacer: esa hora ya fue ocupada por otra cita.")
