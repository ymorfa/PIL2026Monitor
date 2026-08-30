# Monitor de la convocatoria PIL 2026

Este proyecto revisa cada 30 minutos la [convocatoria del Programa de Inserción Laboral 2026 de SECIHTI](https://www.secihti.mx/convocatoria/ciencias-y-humanidades/programa-de-insercion-laboral-pil/convocatoria-del-programa-de-insercion-laboral-2026/) y envía una alerta a Telegram cuando detecta contenido nuevo o modificado.

El monitor no calcula el hash del HTML completo. El sitio agrega parámetros dinámicos de caché y seguridad que cambian aunque la convocatoria siga igual. En su lugar, extrae el texto y los enlaces del bloque específico de la convocatoria, ignora scripts/estilos/formularios y calcula un SHA-256 de ese contenido estable. Así detecta un aviso, texto nuevo o una URL de PDF agregada/modificada sin avisar por cambios cosméticos en el encabezado o pie de página. No descarga ni compara el contenido binario de los PDFs.

## Qué incluye

- Revisión inmediata al arrancar y después cada 30 minutos.
- Primera ejecución como línea base, sin falsa alerta por los avisos ya publicados.
- Comparación de texto y destinos de enlaces/PDF.
- Resumen de líneas agregadas y eliminadas en Telegram.
- Cola de salida persistente: un cambio observado no se pierde si Telegram está caído o si la página revierte después.
- Reintentos con espera progresiva para errores temporales de SECIHTI.
- Validación del contenido para no reemplazar la línea base con una página de error o bloqueo.
- Persistencia atómica en `.state/monitor_state.json`.
- Bloqueo para evitar dos monitores simultáneos.
- Alerta operativa después de tres revisiones fallidas consecutivas y aviso de recuperación.
- Modos `--once`, `--dry-run` y `--test-telegram`.

## 1. Crear el bot de Telegram

1. Abre Telegram y entra al bot oficial [@BotFather](https://t.me/BotFather). Comprueba que el usuario sea exactamente `@BotFather` y tenga la verificación de Telegram.
2. Envía `/newbot`.
3. Escribe el nombre visible del bot, por ejemplo `Monitor PIL 2026`.
4. Elige un nombre de usuario único que termine en `bot`, por ejemplo `mi_monitor_pil_2026_bot`.
5. BotFather entregará un token parecido a `123456789:AA...`. Trátalo como contraseña: no lo publiques ni lo envíes a otras personas.
6. Abre el enlace de tu bot, pulsa **Iniciar** o envíale `/start`. Un bot no puede iniciar la conversación por su cuenta.

La documentación oficial de Telegram explica la [creación de bots con BotFather](https://core.telegram.org/bots/features#botfather) y el método [`sendMessage`](https://core.telegram.org/bots/api#sendmessage) usado por este proyecto.

## 2. Obtener el `chat_id`

Después de enviar `/start` a tu bot, abre esta dirección en el navegador, sustituyendo `<TOKEN>` por el token real:

```text
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Busca una sección similar a esta:

```json
{
  "message": {
    "chat": {
      "id": 123456789,
      "type": "private"
    }
  }
}
```

El número de `chat.id` es el valor para `TELEGRAM_CHAT_ID`. Si `result` está vacío, vuelve a enviar un mensaje al bot y recarga la dirección. Consulta la referencia oficial de [`getUpdates`](https://core.telegram.org/bots/api#getupdates) si necesitas más detalle.

Para notificar a un grupo:

1. Agrega el bot al grupo.
2. Envía `/start@nombre_de_tu_bot` dentro del grupo.
3. Vuelve a consultar `getUpdates`.
4. Usa el `chat.id` del grupo; normalmente es un número negativo.

La URL de `getUpdates` contiene el token. Evita compartir capturas de pantalla y cierra la pestaña al terminar. Si el token se filtra, usa BotFather para revocarlo y generar otro.

## 3. Configurar `.env`

El archivo `.env` ya está creado. Abrelo y completa estas dos líneas:

```dotenv
TELEGRAM_BOT_TOKEN=pega_aqui_el_token_de_BotFather
TELEGRAM_CHAT_ID=pega_aqui_el_chat_id
```

Protege el archivo en Linux:

```bash
chmod 600 .env
```

`.env` está excluido en `.gitignore`; nunca lo agregues manualmente a Git. `.env.example` sirve como plantilla sin secretos.

La frecuencia ya está configurada en 30 minutos:

```dotenv
CHECK_INTERVAL_MINUTES=30
```

Los demás valores normalmente no necesitan cambios:

| Variable | Función | Valor inicial |
| --- | --- | --- |
| `TARGET_URL` | Página que se monitorea | Convocatoria PIL 2026 |
| `CHECK_INTERVAL_MINUTES` | Minutos entre inicios de revisión | `30` |
| `REQUEST_CONNECT_TIMEOUT_SECONDS` | Tiempo máximo para conectar | `10` |
| `REQUEST_READ_TIMEOUT_SECONDS` | Tiempo máximo para recibir datos | `30` |
| `REQUEST_RETRIES` | Reintentos ante 429/errores 5xx | `4` |
| `CSS_SELECTOR` | Bloque relevante dentro del HTML | Post `58580` |
| `EXPECTED_TEXT` | Validación contra páginas de error | `Inserción Laboral 2026` |
| `STATE_FILE` | Línea base persistente | `.state/monitor_state.json` |
| `FAILURE_ALERT_THRESHOLD` | Fallos consecutivos antes de alertar | `3` |
| `LOG_LEVEL` | Detalle de los logs | `INFO` |

## 4. Instalar dependencias

Ya existe el entorno virtual `.venv`. Desde la carpeta del proyecto ejecuta:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

En una instalación nueva, si `.venv` no existiera:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## 5. Probar la configuración

Primero comprueba la extracción de la página. Este modo no requiere Telegram, no escribe el estado y muestra la huella y el contenido monitoreado:

```bash
.venv/bin/python monitor.py --dry-run
```

Después envía un mensaje de prueba:

```bash
.venv/bin/python monitor.py --test-telegram
```

Finalmente haz una sola revisión:

```bash
.venv/bin/python monitor.py --once
```

La primera revisión válida crea `.state/monitor_state.json` y no manda una alerta de cambio. Esto evita reportar como nuevos los avisos que ya existen. El mensaje de `--test-telegram` confirma por separado que el bot quedó configurado.

## 6. Ejecutar cada 30 minutos

### Opción sencilla: proceso continuo

```bash
.venv/bin/python monitor.py
```

Revisa la página inmediatamente y mantiene el proceso abierto. Para detenerlo usa `Ctrl+C`. La terminal debe permanecer abierta.

### Opción recomendada: servicio systemd del usuario

Esta opción reinicia el monitor si falla y puede iniciarlo con tu sesión.

1. Crea la carpeta de servicios del usuario:

   ```bash
   mkdir -p ~/.config/systemd/user
   ```

2. Copia la plantilla:

   ```bash
   cp systemd/pil-monitor.service.example ~/.config/systemd/user/pil-monitor.service
   ```

3. Edita el archivo copiado y reemplaza las tres apariciones de `/RUTA/ABSOLUTA/PLI` por la ruta real de este proyecto. Puedes obtenerla con `pwd`.
4. Recarga e inicia el servicio:

   ```bash
   systemctl --user daemon-reload
   systemctl --user enable --now pil-monitor.service
   ```

5. Revisa su estado y logs:

   ```bash
   systemctl --user status pil-monitor.service
   journalctl --user -u pil-monitor.service -f
   ```

Para detenerlo y evitar que arranque automáticamente:

```bash
systemctl --user disable --now pil-monitor.service
```

En algunas distribuciones los servicios de usuario se detienen al cerrar sesión. Para mantenerlo activo sin una sesión abierta, habilita *linger* una vez:

```bash
sudo loginctl enable-linger "$USER"
```

### Alternativa: cron

El modo `--once` permite ejecutar una revisión a los minutos 0 y 30 de cada hora. Abre `crontab -e` y agrega una línea con rutas absolutas:

```cron
*/30 * * * * cd /RUTA/ABSOLUTA/PLI && .venv/bin/python monitor.py --once >> monitor.log 2>&1
```

El propio programa ya aplica un bloqueo de instancia, también en modo `--once`. Usa solamente un método: proceso continuo, systemd o cron. Dos métodos simultáneos no aportan revisiones extra y el bloqueo impedirá que compartan el mismo estado al mismo tiempo.

## Cómo funciona una alerta

Al detectar una nueva huella, el monitor compara el snapshot anterior con el nuevo, clasifica términos como `resultados`, `dictamen`, `seleccionados` o `aviso`, y manda a Telegram:

- la hora de detección;
- la URL oficial;
- líneas agregadas o actualizadas;
- líneas reemplazadas o eliminadas.

Cuando observa una transición, primero la guarda como evento pendiente en el archivo de estado y después contacta Telegram. Sólo elimina ese evento cuando Telegram confirma el mensaje. Si el envío falla, vuelve a intentarlo en la siguiente revisión; si mientras tanto la página cambia otra vez o regresa al estado anterior, también conserva esa segunda transición. Cada mensaje lleva un identificador de evento para reconocer reintentos.

Este comportamiento prioriza no perder alertas. En el caso poco común de que Telegram reciba el mensaje pero se pierda su respuesta antes de que el monitor pueda confirmar y guardar la entrega, podría llegar un duplicado con el mismo identificador.

## Mantenimiento y solución de problemas

### `getUpdates` devuelve una lista vacía

Envía `/start` o cualquier mensaje nuevo al bot y vuelve a cargar. Si antes configuraste un webhook, elimínalo antes de usar `getUpdates`.

### Telegram responde `Unauthorized`

El token es incorrecto o fue revocado. Copia el token actual de BotFather sin espacios ni comillas.

### Telegram responde `chat not found`

Comprueba el `chat_id` y asegúrate de haber iniciado la conversación. Para grupos, verifica que el bot siga dentro del grupo.

### No se encontró el bloque principal

SECIHTI pudo cambiar la plantilla del sitio o devolvió una página de seguridad. El monitor no reemplaza la línea base en ese caso. Ejecuta:

```bash
.venv/bin/python monitor.py --dry-run
```

Si la página oficial carga pero el error persiste, inspecciona el HTML y actualiza `CSS_SELECTOR` en `.env`.

### El archivo de estado está dañado

El monitor se detiene en vez de sobrescribir un estado ilegible, porque hacerlo podría perder eventos pendientes. Revisa el error en los logs, detén el servicio y respalda el archivo antes de restablecerlo con el procedimiento siguiente.

### Restablecer intencionalmente la línea base

Detén primero el servicio. Haz un respaldo y mueve el archivo de estado:

```bash
mv .state/monitor_state.json .state/monitor_state.backup.json
```

En el siguiente arranque se creará una línea base nueva sin alerta. No borres o muevas el estado mientras el monitor esté ejecutándose.

### Ejecutar las pruebas automatizadas

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Las pruebas cubren scripts dinámicos, cambios puramente visuales de marcado, una sección realista de resultados, cambios de URL con la misma etiqueta, validación de páginas incorrectas, el límite de Telegram, respuestas malformadas, protección del token en logs, escritura del estado y reintentos durables incluso cuando la página revierte.

## Seguridad

- Nunca compartas `TELEGRAM_BOT_TOKEN` ni lo incluyas en capturas, logs o commits.
- El monitor valida TLS y no desactiva certificados HTTPS.
- Las excepciones de red de Telegram se redactan para evitar imprimir accidentalmente la URL que contiene el token.
- Incluso con `LOG_LEVEL=DEBUG`, los logs internos de `requests`/`urllib3` se mantienen en `WARNING` porque la ruta de la API contiene el token.
- Si crees que el token se expuso, revócalo inmediatamente desde BotFather y actualiza `.env`.
