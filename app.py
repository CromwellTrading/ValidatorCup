import re
import json
import requests
import os
from datetime import datetime
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==============================================================================
# 0. CARGA DE CONFIGURACIÓN (.ENV)
# ==============================================================================

# 1. SEGURIDAD: ¿Quién puede entrar aquí? (Tokens para Deku)
# Ejemplo: {"juan123": "Celular_Juan", "pepe456": "Celular_Pepe"}
try:
    auth_env = os.environ.get("AUTHORIZED_TOKENS", "{}")
    VALID_TOKENS = json.loads(auth_env)
except json.JSONDecodeError:
    print("⚠️ Error: AUTHORIZED_TOKENS mal formado. Acceso bloqueado.")
    VALID_TOKENS = {}

# 2. ENRUTAMIENTO: ¿A dónde envío el JSON limpio?
# Mapea el número receptor (tu tarjeta/celular) con la URL del siguiente servicio.
# Ejemplo: {"5350000000": "https://api.tu-otro-servicio.com/procesar-juan"}
try:
    routes_env = os.environ.get("CLIENT_ROUTES", "{}")
    CLIENT_ROUTES = json.loads(routes_env)
except json.JSONDecodeError:
    print("⚠️ Error: CLIENT_ROUTES mal formado. No se podrá reenviar.")
    CLIENT_ROUTES = {}

# ==============================================================================
# 1. MOTORES DE PARSEO (CEREBRO)
# ==============================================================================

def parse_transfermovil(text):
    data = {
        "proveedor": "TRANSFERMOVIL",
        "tipo_transaccion": "DESCONOCIDO",
        "monto": 0.0,
        "remitente": None, "receptor": None, "transaccion_id": None,
        "valid": False, "raw": text
    }
    
    # Intento 1: Pago Identificado (Titular a cuenta)
    regex_full = r"titular del tel[eé]fono\s+(\d+).*transferencia\s+(?:a la cuenta|al Monedero MiTransfer)\s+([\dX]+)\s+de\s+([\d.]+)\s+CUP.*Nro. Transaccion\s+([A-Z0-9]+)"
    match_full = re.search(regex_full, text, re.IGNORECASE | re.DOTALL)

    if match_full:
        data["remitente"] = match_full.group(1)
        data["receptor"] = match_full.group(2)
        data["monto"] = float(match_full.group(3))
        data["transaccion_id"] = match_full.group(4)
        data["tipo_transaccion"] = "PAGO_IDENTIFICADO"
        data["valid"] = True
        return data

    # Intento 2: Pago Monedero (A veces llega sin remitente claro en algunas versiones)
    if "Monedero MiTransfer" in text:
        match_monto = re.search(r"(?:con:|de)\s*([\d\.]+)\s*CUP", text)
        match_id = re.search(r"(?:Id|Nro\.)\s*Transaccion[:\s]+([A-Z0-9]+)", text, re.IGNORECASE)
        
        if match_monto and match_id:
            data["remitente"] = "ANONIMO"
            data["receptor"] = "MONEDERO_DETECTADO" # Se resolverá después
            data["monto"] = float(match_monto.group(1))
            data["transaccion_id"] = match_id.group(1)
            data["tipo_transaccion"] = "PAGO_ANONIMO"
            data["valid"] = True
            return data
            
    return data

def parse_cubacel(text):
    data = {"proveedor": "CUBACEL", "valid": False}
    match = re.search(r"recibido\s+([\d.]+)\s+CUP\s+del\s+numero\s+(\d+)", text, re.IGNORECASE)
    
    if match:
        data["monto"] = float(match.group(1))
        data["remitente"] = match.group(2)
        data["tipo_transaccion"] = "SALDO_RECIBIDO"
        data["valid"] = True
        return data
    return data

# ==============================================================================
# 2. ENDPOINT PRINCIPAL (WEBHOOK)
# ==============================================================================

@app.route('/webhook/<token>', methods=['POST'])
def sms_gateway(token):
    # --- 🔒 1. VALIDACIÓN DEL TOKEN EN URL ---
    if token not in VALID_TOKENS:
        print(f"⛔ ACCESO DENEGADO. Token inválido: {token}")
        return jsonify({"status": "error", "msg": "Unauthorized"}), 401

    cliente_origen = VALID_TOKENS[token]
    print(f"✅ SMS de: {cliente_origen}")

    # --- 📥 2. RECIBIR DATA ---
    try:
        req = request.get_json(force=True, silent=True)
        if not req:
            return jsonify({"status": "error", "msg": "No JSON"}), 400
        
        sms_text = req.get("text") or req.get("body") or req.get("message") or ""
        sender_origin = req.get("dirección", "") or req.get("sender", "") or ""
        # El número que recibió el SMS (útil si Deku lo envía, sino usamos default)
        my_receiver_number = req.get("my_number", "NUMERO_DESCONOCIDO")

        print(f"📨 RAW: {sms_text[:50]}...")

        # --- 🧠 3. PARSEO ---
        parsed_data = {}
        
        # Detectar tipo de mensaje
        if "PAGO" in sender_origin.upper() or "TRANSFER" in sms_text.upper():
            parsed_data = parse_transfermovil(sms_text)
        elif "CUBACEL" in sender_origin.upper() or "CUBACEL" in sms_text.upper():
            parsed_data = parse_cubacel(sms_text)
            parsed_data["receptor"] = my_receiver_number # Cubacel no dice a quién se lo enviaste (es a ti mismo)
        
        # Si no se pudo leer, ignoramos
        if not parsed_data.get("valid"):
            print("⚠️ SMS ignorado (No coincide con patrones).")
            return jsonify({"status": "ignored"}), 200

        # --- 🔀 4. RESOLUCIÓN DE DESTINO (ROUTING) ---
        receptor_final = parsed_data.get("receptor")

        # Caso especial: Monedero no dice el número de cuenta en el SMS a veces
        if receptor_final == "MONEDERO_DETECTADO":
            # Aquí podrías asignar uno por defecto o buscar en CLIENT_ROUTES si tienes lógica extra
            # Por ahora lo dejaremos pasar tal cual
            pass

        # Buscar a qué URL enviar este JSON
        destination_url = CLIENT_ROUTES.get(str(receptor_final))

        # Si no hay match exacto, buscar parcial (útil para tarjetas que cambian o claves largas)
        if not destination_url:
            for key_account, url in CLIENT_ROUTES.items():
                if key_account in str(receptor_final):
                    destination_url = url
                    parsed_data["receptor_normalizado"] = key_account
                    break
        
        if not destination_url:
            print(f"❌ Error: No tengo a dónde enviar datos de la cuenta {receptor_final}")
            # Guardamos el log pero respondemos 200 a Deku para que no reintente
            return jsonify({"status": "error", "msg": "No route for receiver"}), 200

        # --- 🚀 5. REENVÍO AL SIGUIENTE SERVICIO ---
        payload_forward = {
            "source": "sms_parser",
            "timestamp": datetime.now().isoformat(),
            "origin_device": cliente_origen,
            "data": parsed_data
        }

        print(f"🚀 Reenviando a {destination_url}...")
        
        # Enviamos y olvidamos (Fire and forget) o esperamos respuesta rápida
        try:
            requests.post(destination_url, json=payload_forward, timeout=5)
            print("✅ JSON enviado exitosamente.")
        except Exception as e:
            print(f"⚠️ Falló el reenvío al servicio final: {e}")

        # Respondemos a Deku que todo salió bien (ya nosotros tenemos la data)
        return jsonify({"status": "success", "parsed": True}), 200

    except Exception as e:
        print(f"🔥 CRITICAL ERROR: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
