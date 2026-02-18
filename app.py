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

# 1. SEGURIDAD: Tokens autorizados
try:
    auth_env = os.environ.get("AUTHORIZED_TOKENS", "{}")
    VALID_TOKENS = json.loads(auth_env)
except json.JSONDecodeError:
    print("⚠️ Error: AUTHORIZED_TOKENS mal formado. Acceso bloqueado.")
    VALID_TOKENS = {}

# 2. ENRUTAMIENTO NORMAL: mapea número receptor -> URL destino
try:
    routes_env = os.environ.get("CLIENT_ROUTES", "{}")
    CLIENT_ROUTES = json.loads(routes_env)
except json.JSONDecodeError:
    print("⚠️ Error: CLIENT_ROUTES mal formado. No se podrá reenviar.")
    CLIENT_ROUTES = {}

# 3. RUTA DE DEPURACIÓN (opcional): todos los mensajes no ruteables se envían aquí
DEBUG_ROUTE = os.environ.get("DEBUG_ROUTE")  # Ej: "https://mi-servidor.com/debug"

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
    print(f"✅ SMS de: {cliente_origen} (token: {token})")

    # --- 📥 2. RECIBIR DATA ---
    try:
        req = request.get_json(force=True, silent=True)
        if not req:
            return jsonify({"status": "error", "msg": "No JSON"}), 400
        
        sms_text = req.get("text") or req.get("body") or req.get("message") or ""
        sender_origin = req.get("dirección", "") or req.get("sender", "") or ""
        my_receiver_number = req.get("my_number", "NUMERO_DESCONOCIDO")

        print(f"📨 RAW (primeros 100 chars): {sms_text[:100]}...")
        print(f"📞 Remitente original: {sender_origin}")
        print(f"📲 Número receptor propio: {my_receiver_number}")

        # --- 🧠 3. PARSEO ---
        parsed_data = {}
        
        # Detectar tipo de mensaje basado en el remitente original o el texto
        if "PAGO" in sender_origin.upper() or "TRANSFER" in sms_text.upper():
            parsed_data = parse_transfermovil(sms_text)
        elif "CUBACEL" in sender_origin.upper() or "CUBACEL" in sms_text.upper():
            parsed_data = parse_cubacel(sms_text)
            parsed_data["receptor"] = my_receiver_number # Cubacel no dice a quién se lo enviaste (es a ti mismo)
        else:
            # Si no se pudo determinar el proveedor, creamos un objeto mínimo
            parsed_data = {
                "proveedor": "DESCONOCIDO",
                "valid": False,
                "raw": sms_text
            }

        # --- 🔀 4. RESOLUCIÓN DE DESTINO ---
        destination_url = None
        receptor_final = parsed_data.get("receptor") if isinstance(parsed_data, dict) else None

        # Si el parseo fue válido y tenemos receptor, intentamos enrutamiento normal
        if parsed_data.get("valid") and receptor_final:
            # Buscar coincidencia exacta
            destination_url = CLIENT_ROUTES.get(str(receptor_final))
            # Si no, buscar coincidencia parcial
            if not destination_url:
                for key_account, url in CLIENT_ROUTES.items():
                    if key_account in str(receptor_final):
                        destination_url = url
                        parsed_data["receptor_normalizado"] = key_account
                        break

        # --- 🚀 5. REENVÍO (normal o a depuración) ---
        payload_forward = {
            "source": "sms_parser",
            "timestamp": datetime.now().isoformat(),
            "origin_device": cliente_origen,
            "token": token,  # útil para depuración
            "sender_original": sender_origin,
            "my_receiver_number": my_receiver_number,
            "data": parsed_data
        }

        # Si tenemos destino normal, enviamos allí
        if destination_url:
            print(f"🚀 Reenviando a destino normal: {destination_url}")
            try:
                requests.post(destination_url, json=payload_forward, timeout=5)
                print("✅ JSON enviado exitosamente a destino normal.")
            except Exception as e:
                print(f"⚠️ Falló el reenvío al destino normal: {e}")
        else:
            # No hay ruta normal: si DEBUG_ROUTE está configurada, enviamos a depuración
            if DEBUG_ROUTE:
                print(f"🔍 No hay ruta normal. Enviando a depuración: {DEBUG_ROUTE}")
                try:
                    requests.post(DEBUG_ROUTE, json=payload_forward, timeout=5)
                    print("✅ JSON enviado a depuración.")
                except Exception as e:
                    print(f"⚠️ Falló el envío a depuración: {e}")
            else:
                print("ℹ️ No hay ruta normal ni DEBUG_ROUTE. Mensaje no reenviado.")

        # Respondemos siempre 200 a Deku para que no reintente
        return jsonify({"status": "success", "parsed": parsed_data.get("valid", False)}), 200

    except Exception as e:
        print(f"🔥 CRITICAL ERROR: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
