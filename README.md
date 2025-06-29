# socialbot

Instrucciones:

subir el archivo php y config a un hosting
crear cuenta gratis en: https://rapidapi.com/
suscribir a https://rapidapi.com/irrors-apis/api/instagram-looter2
te da gratis 150 queries/mes

crear una cuenta developer en X: https://developer.x.com
una vez creada y aprobada van al default project en la columna de la izquierda y abajo del mismo les va a aparecer un numero que finaliza con su alias de X
en esa seccion van a key and tokens y generan su bearer token, lo copian y lo pegan en el config.txt


En telegram, con botfather crear un bot y poner el token en el config.txt

finalmente

Una vez subidos los archivos, configurar el webhook de Telegram para que apunte a tu bot. 
Visita esta URL en tu navegador (reemplaza los valores):

https://api.telegram.org/bot[TU_TOKEN]/setWebhook?url=https://[TU_DOMINIO]/bot.php

por ejemplo:

https://api.telegram.org/bot7435666643:AAE86MML8pGjvovfdhhhdT/setWebhook?url=https://midominio.com/bot.php

una vez que esto da TRUE

deberia andar.
