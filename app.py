from flask import Flask, request
import json

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def gateway():
    # Deku SMS suele enviar los datos en formato JSON
    try:
        data = request.get_json()
        
        # Logs para ver qué llega exactamente (Esto lo verás en Render)
        print("--- NUEVO MENSAJE RECIBIDO ---")
        print(json.dumps(data, indent=2))

        # Extraemos lo básico (ajustaremos los nombres según lo que veamos en el log)
        sender = data.get("from", "Desconocido")
        message = data.get("text", "")

        # FILTRO DE SEGURIDAD
        if "PAGO-MOVIL" in sender.upper() or "PAGOS" in sender.upper():
            print(f"✅ ¡PAGO DETECTADO! De: {sender}")
            print(f"Contenido: {message}")
            # Aquí es donde luego pondremos la lógica para avisar a tu otro bot
        else:
            print(f"❌ Mensaje ignorado de: {sender}")

    except Exception as e:
        print(f"Error procesando datos: {e}")

    return "OK", 200

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)
