"""Ejecutar con:  python -m unittest -v"""
import unittest
from datetime import datetime, date

import nucleo as n

AHORA = datetime(2026, 11, 20, 12, 0)
HOY = date(2026, 11, 20)
LUNES_9 = "2026-11-23 09:00"


class Base(unittest.TestCase):
    def setUp(self):
        self.con = n.conectar()
        self.con.execute("INSERT INTO clinicas (id, nombre) VALUES (1,'Clínica Centro'),(2,'Clínica Norte')")
        self.con.execute("INSERT INTO medicos (id, nombre) VALUES (1,'Dr. Salazar')")
        self.p1 = n.registrar_paciente(self.con, "rosa", "Juan Pérez", "1980-05-10", hoy=HOY)
        self.p2 = n.registrar_paciente(self.con, "rosa", "Ana Torres", "1990-01-02", hoy=HOY)


class TestValidaciones(unittest.TestCase):
    def test_cedula_valida(self):
        self.assertEqual(n.validar_cedula("1710034065"), "1710034065")

    def test_cedula_invalida(self):
        for c in ["1710034064", "000000000", "0000000000", "17100340AB", "1111111111", ""]:
            with self.assertRaises(n.ErrorNegocio, msg=c):
                n.validar_cedula(c)

    def test_nombres_equivalentes(self):
        self.assertEqual(n.clave_nombre("  MARÍA   lópez "), n.clave_nombre("Maria Lopez"))

    def test_nombre_con_basura(self):
        for x in ["😀😀😀", "'; DROP TABLE citas;--", "<script>x</script>", "12345"]:
            with self.assertRaises(n.ErrorNegocio, msg=x):
                n.limpiar_nombre(x)

    def test_telefono_y_correo(self):
        self.assertEqual(n.validar_telefono("+593 99 123 4567"), "0991234567")
        with self.assertRaises(n.ErrorNegocio):
            n.validar_telefono("12345")
        with self.assertRaises(n.ErrorNegocio):
            n.validar_correo("juan@@gmail")
        self.assertIsNone(n.validar_correo(""))

    def test_fechas_nacimiento(self):
        for f in ["2090-01-01", "1700-01-01", "31/12/1990", "basura"]:
            with self.assertRaises(n.ErrorNegocio, msg=f):
                n.validar_fecha_nacimiento(f, HOY)


class TestPacientes(Base):
    def test_paciente_sin_nada_de_contacto(self):
        pid = n.registrar_paciente(self.con, "rosa", "Niño Sin Datos", "2018-03-03", hoy=HOY)
        self.assertGreater(pid, 0)

    def test_homonimo_pide_confirmacion(self):
        with self.assertRaises(n.ErrorNegocio):
            n.registrar_paciente(self.con, "rosa", "JUAN  pérez", "1980-05-10", hoy=HOY)
        n.registrar_paciente(self.con, "rosa", "JUAN  pérez", "1980-05-10",
                             confirmar_homonimo=True, hoy=HOY)

    def test_documento_repetido(self):
        n.registrar_paciente(self.con, "rosa", "Luis Mora", "1970-01-01", "cedula", "1710034065", hoy=HOY)
        with self.assertRaises(n.ErrorNegocio):
            n.registrar_paciente(self.con, "rosa", "Otro Nombre", "1971-01-01", "cedula", "1710034065", hoy=HOY)


class TestCitas(Base):
    def agendar(self, pac=None, clinica=1, inicio=LUNES_9):
        return n.agendar_cita(self.con, "rosa", pac or self.p1, 1, clinica, inicio, ahora=AHORA)

    def test_cita_en_el_pasado(self):
        with self.assertRaises(n.ErrorNegocio):
            self.agendar(inicio="2026-11-19 09:00")

    def test_misma_hora_mismo_medico(self):
        self.agendar()
        with self.assertRaises(n.ErrorNegocio):
            self.agendar(pac=self.p2)

    def test_medico_en_dos_clinicas_a_la_vez(self):
        self.agendar(clinica=1)
        with self.assertRaises(n.ErrorNegocio) as e:
            self.agendar(pac=self.p2, clinica=2, inicio="2026-11-23 09:15")  # se solapa
        self.assertIn("Clínica Centro", str(e.exception))

    def test_citas_seguidas_no_chocan(self):
        self.agendar()
        self.agendar(pac=self.p2, inicio="2026-11-23 09:30")

    def test_cancelar_dos_veces_y_liberar_hora(self):
        cid = self.agendar()
        self.assertIn("cancelada", n.cancelar_cita(self.con, "rosa", cid))
        self.assertIn("ya estaba", n.cancelar_cita(self.con, "rosa", cid))
        n_aud = self.con.execute("SELECT COUNT(*) FROM auditoria WHERE accion='cancelar'").fetchone()[0]
        self.assertEqual(n_aud, 1)          # la segunda vez no ensucia la auditoría
        self.agendar(pac=self.p2)           # la hora quedó libre

    def test_deshacer_cancelacion(self):
        cid = self.agendar()
        n.cancelar_cita(self.con, "rosa", cid)
        self.assertEqual(n.deshacer_cancelacion(self.con, "rosa", cid, ahora=AHORA), "Cita reactivada.")
        n.cancelar_cita(self.con, "rosa", cid)
        self.agendar(pac=self.p2)           # alguien ocupa la hora
        with self.assertRaises(n.ErrorNegocio):
            n.deshacer_cancelacion(self.con, "rosa", cid, ahora=AHORA)

    def test_la_base_de_datos_tambien_protege(self):
        """Aunque el código fallara, el índice único impide la doble reserva."""
        self.agendar()
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO citas (paciente_id, medico_id, clinica_id, inicio, fin, creado_por, creado_en)"
                " VALUES (2,1,1,'2026-11-23T09:00','2026-11-23T09:30','x','x')")

    def test_auditoria_registra_quien(self):
        cid = self.agendar()
        fila = self.con.execute("SELECT usuario, accion FROM auditoria WHERE entidad='cita' AND entidad_id=?",
                                (cid,)).fetchone()
        self.assertEqual((fila["usuario"], fila["accion"]), ("rosa", "agendar"))


if __name__ == "__main__":
    unittest.main()
