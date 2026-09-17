# Hue Studio

Aplicación local en **Python**, con interfaz web multilingüe, para administrar Philips Hue y guardar/restaurar configuraciones. Sin dependencias Python ni compilación del frontend. Requiere Python 3.10 o posterior y un navegador moderno.

La interfaz está disponible en **español, inglés y francés**. Usa el selector de la cabecera para cambiar de idioma; la elección se recuerda en este navegador. Los nombres de luces, habitaciones y escenas mantienen el texto del bridge.

```bash
python3 app.py
```

Abre automáticamente **http://127.0.0.1:8765**. Para detenerla, `Ctrl+C` en la terminal.

```bash
python3 app.py --no-browser --port 8765
python3 -m unittest discover -s tests -v
```

## Primer uso

1. Arranca la aplicación. Si existe `hue-export.json`, podrás explorar sus datos sin conectarte ni cambiar dispositivos.
2. Pulsa **Conectar bridge**. Selecciona una conexión guardada, descubre bridges o introduce su IP local.
3. Para una conexión nueva, pulsa el botón físico del bridge y después **Vincular**. Si tienes una clave, puedes introducirla y pulsar **Conectar**.
4. Crea una copia completa antes de reorganizar tu instalación.

Se reutiliza la conexión de `.hue-config.json`, si existe. Puedes guardar varios bridges y cambiar entre ellos; se administra uno cada vez. El descubrimiento usa Internet, pero la conexión por IP y el control son locales. La vista se actualiza con **Actualizar** o después de cada modificación; no hay sincronización automática con cambios externos.

## Funciones

- Panel general con habitaciones, luces, sensores y escenas.
- Encendido, brillo, temperatura y color cuando el dispositivo los admite; identificación mediante destello.
- Consulta de sensores, batería, modelo, estado y todas sus propiedades JSON.
- Creación y edición de habitaciones, zonas y grupos, seleccionando sus luces.
- Activación de escenas y creación de escenas con el ambiente actual.
- Mapa navegable de relaciones entre bridge, espacios y luces, con búsqueda. Al pulsar un elemento se resaltan sus conexiones y se muestran sus luces, espacios, escenas y sensores relacionados.
- Explorador de luces, sensores, grupos, escenas, horarios, reglas y enlaces; editor de propiedades compatibles con la API. Los campos de solo lectura son rechazados por el bridge.
- Copias completas con detalle individual de las escenas, importación/exportación JSON y restauración con vista previa.
- Copia previa automática antes de restaurar o eliminar un recurso lógico.

## Qué significa restaurar

La restauración es **parcial y no destructiva respecto a recursos sobrantes**: no borra la instalación existente ni equivale a clonar un bridge. Sí puede cambiar luces, ajustes y pertenencias a habitaciones. Antes de ejecutarla se muestran las peticiones previstas y cada recurso que se omitirá.

| Recurso | Tratamiento |
| --- | --- |
| Luces | Coincidencia por `uniqueid` físico; recupera nombre, encendido, brillo y color/temperatura guardados. |
| Sensores | Coincidencia por `uniqueid`; nombre y ajustes básicos presentes (`on`, `ledindication`, `sensitivity`, `usertest`). |
| Habitaciones, zonas y grupos normales | Actualiza los coincidentes o crea los ausentes. Remapea las luces. Omite grupos con dispositivos no resueltos o sensores asociados. |
| Escenas con `lightstates` | Crea **escenas nuevas** y remapea luces y grupo. Repetir una restauración puede generar duplicados. |
| Escenas sin `lightstates` | Las omite e informa del motivo. Las exportaciones antiguas pueden no contener estos datos. |
| Reglas, horarios, enlaces | Guarda sus datos en el backup; no los restaura automáticamente. Sus referencias y credenciales requieren revisión manual. |
| Grupos Entertainment | Conserva los datos; configuración manual necesaria. |
| Usuarios, red, Zigbee, integraciones, firmware | No los restaura. No recupera emparejamientos ni claves de otras aplicaciones. |

Para restaurar en otro bridge, primero empareja físicamente los dispositivos en el destino mediante Hue. Los identificadores numéricos no son suficientes: Hue Studio exige coincidencia física única para evitar modificar una luz equivocada. Una luz solo puede pertenecer a una habitación; restaurar habitaciones puede cambiar asignaciones existentes.

La API no ofrece una transacción global. Ante un error se detienen las operaciones siguientes y se muestra el progreso realizado; una petición también puede haber aplicado parte de sus campos antes de devolver un error. **No hay rollback automático**. La copia previa queda disponible para inspección/recuperación dentro de los mismos límites. El informe más reciente se escribe en `.hue-studio/last-restore.json`. Las vistas previas caducan a los diez minutos y se invalidan al cambiar de bridge.

## Compatibilidad y alcance

Esta versión utiliza la **API local v1 de Hue**, adecuada para la exportación existente del proyecto. No utiliza los recursos exclusivos de API v2: Matter, Secure, efectos avanzados, escenas dinámicas o todas las automatizaciones modernas pueden quedar fuera de la copia y de la gestión. No se promete compatibilidad universal con cualquier hardware Hue, dispositivos solo Bluetooth o futuras versiones. No hay control remoto por cuenta Hue ni emparejamiento Zigbee desde esta aplicación.

Se usa HTTPS por defecto y se permite HTTP explícitamente para bridges antiguos. Los certificados locales del bridge se aceptan sin verificar su cadena; esta excepción solo se aplica al cliente de IP privada del bridge. Úsalo en una red local de confianza. Philips documenta la evolución de HTTPS y las diferencias entre APIs en [New Hue API](https://developers.meethue.com/new-hue-api/) y la vinculación local en [Get started](https://developers.meethue.com/develop/get-started-2/).

## Datos y privacidad

- El servidor solo escucha en `127.0.0.1`; no se expone a la LAN.
- Las mutaciones requieren un token de sesión y se comprueban `Host` y `Origin`.
- Las credenciales se guardan localmente en `.hue-studio/bridges.json`, con permisos de archivo `0600`; no están cifradas en disco.
- Las copias se guardan en `.hue-studio/backups/`, también con permisos `0600`. No contienen la whitelist ni las claves de aplicación; las rutas de automatización se exportan con la clave sustituida por un marcador.
- Las copias sí contienen información del hogar, direcciones e identificadores de dispositivos: guárdalas en un lugar privado.
- El frontend no usa CDN, fuentes externas ni servicios de telemetría.
- `.gitignore` excluye exportaciones, configuración, credenciales y copias.

Puedes cambiar las rutas:

```bash
python3 app.py --data-dir /ruta/privada/hue --export /ruta/hue-export.json
```

## Estructura

```text
app.py                    Entrada
hue_studio/bridge.py       Cliente Hue y planificación de restauración
hue_studio/server.py       Servidor local, conexiones y copias
hue_studio/static/         Interfaz HTML, CSS y JavaScript
tests/test_studio.py       Pruebas de restauración, errores y servidor
```

Las pruebas usan bridges simulados y un servidor HTTP temporal en localhost: no modifican dispositivos físicos. La compatibilidad real debe verificarse con el bridge concreto.

## Utilidad Node original

Se conserva la utilidad existente:

```bash
node index.js discover
node index.js token --bridge-ip 192.168.1.10
node index.js export --bridge-ip 192.168.1.10
```

La aplicación Python no depende de Node. Para obtener copias con estados individuales de escenas, usa **Crear copia completa** desde Hue Studio.
