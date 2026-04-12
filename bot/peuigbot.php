<?php
// peuigbot.php - Bot de Telegram para descargar contenido de Instagram y Twitter/X

// Definir constantes
define('LOG_FILE', __DIR__ . '/combined_bot.log');
define('DEBUG_MODE', true); // Ajustar a false en producción

// Función para registrar mensajes en el archivo de log
function writeLog($message) {
    $timestamp = date('Y-m-d H:i:s');
    file_put_contents(LOG_FILE, "[{$timestamp}] {$message}\n", FILE_APPEND);
}

// Función para leer configuración desde config.php
function loadConfig() {
    writeLog("Cargando configuración desde " . __DIR__ . '/config.php');
    $config = include __DIR__ . '/config.php';
    if (!is_array($config)) {
        writeLog('ERROR: Archivo config.php no devuelve un array.');
        error_log('Archivo config.php no devuelve un array');
        return false;
    }
    writeLog("Configuración cargada: " . json_encode($config));
    return $config;
}

// Función para enviar mensajes de texto a Telegram
function sendTelegramMessage($token, $chatId, $message, $parseMode = null) {
    $url = "https://api.telegram.org/bot{$token}/sendMessage";
    $data = ['chat_id' => $chatId, 'text' => $message];
    if ($parseMode) {
        $data['parse_mode'] = $parseMode;
    }
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        writeLog("ERROR al enviar mensaje a Telegram: " . curl_error($ch));
    } else {
        writeLog("Respuesta de Telegram (mensaje): " . $response);
    }
    curl_close($ch);
    return $response;
}

// Descarga un archivo desde una URL CDN al disco local y devuelve ['path', 'content_type'] o false
function downloadFile($url) {
    writeLog("Descargando archivo: {$url}");
    $tmpFile = tempnam(sys_get_temp_dir(), 'tgmedia_');
    $fp = fopen($tmpFile, 'wb');
    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_FILE           => $fp,
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_TIMEOUT        => 180,
        CURLOPT_HTTPHEADER     => [
            'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
            'Referer: https://www.instagram.com/',
        ],
    ]);
    $success = curl_exec($ch);
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    $contentType = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
    curl_close($ch);
    fclose($fp);

    if (!$success || $httpCode !== 200) {
        writeLog("ERROR al descargar (HTTP {$httpCode}): {$url}");
        @unlink($tmpFile);
        return false;
    }
    writeLog("Descargado OK ({$httpCode}), content-type: {$contentType}");
    return ['path' => $tmpFile, 'content_type' => $contentType];
}

// Sube un archivo descargado a Telegram como foto o video
function uploadToTelegram($token, $chatId, $type, $filePath, $contentType, $caption = '') {
    if ($type === 'video') {
        $apiMethod = 'sendVideo';
        $fieldName  = 'video';
        $mime       = $contentType ?: 'video/mp4';
        $fileName   = 'video.mp4';
    } else {
        $apiMethod = 'sendPhoto';
        $fieldName  = 'photo';
        $mime       = $contentType ?: 'image/jpeg';
        $fileName   = 'photo.jpg';
    }

    $url = "https://api.telegram.org/bot{$token}/{$apiMethod}";
    $postData = [
        'chat_id' => $chatId,
        $fieldName => new CURLFile($filePath, $mime, $fileName),
        'caption'  => $caption,
    ];
    if ($type === 'video') {
        $postData['supports_streaming'] = 'true';
    }

    $ch = curl_init($url);
    curl_setopt_array($ch, [
        CURLOPT_POST           => true,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT        => 120,
        CURLOPT_POSTFIELDS     => $postData,
    ]);
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        writeLog("ERROR al subir a Telegram ({$apiMethod}): " . curl_error($ch));
    } else {
        writeLog("Respuesta Telegram ({$apiMethod}): " . $response);
    }
    curl_close($ch);
    return $response;
}

// Función para determinar el tipo de enlace
function getLinkType($url) {
    if (strpos($url, 'instagram.com') !== false || strpos($url, 'instagr.am') !== false) {
        return 'instagram';
    } elseif (strpos($url, 'twitter.com') !== false || strpos($url, 'x.com') !== false) {
        return 'twitter';
    }
    return false;
}

// Construye la URL del proxy en Railway para descargar un archivo CDN.
// Las URLs de Twitter/IG tienen la IP de Railway quemada — hay que descargarlas
// desde Railway, no desde shared hosting.
function buildProxyUrl($mediaUrl, $config) {
    $proxyUrl = rtrim($config['ytdlp_service_url'], '/') . '/proxy?url=' . urlencode($mediaUrl);
    if (!empty($config['ytdlp_secret'])) {
        $proxyUrl .= '&secret=' . urlencode($config['ytdlp_secret']);
    }
    return $proxyUrl;
}

// Llama al microservicio yt-dlp y devuelve un array de items [{type, url}] o false
function getMediaFromYtDlpService($url, $config) {
    if (empty($config['ytdlp_service_url'])) {
        writeLog("ERROR: ytdlp_service_url no configurado.");
        return false;
    }

    $serviceUrl = rtrim($config['ytdlp_service_url'], '/') . '/extract?url=' . urlencode($url);
    writeLog("Llamando al servicio yt-dlp: {$serviceUrl}");

    $headers = ['Accept: application/json'];
    if (!empty($config['ytdlp_secret'])) {
        $headers[] = 'X-Secret: ' . $config['ytdlp_secret'];
    }
    if (!empty($config['ig_cookies'])) {
        $headers[] = 'X-Ig-Cookies: ' . $config['ig_cookies'];
    }

    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $serviceUrl);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    curl_setopt($ch, CURLOPT_TIMEOUT, 60);
    $response = curl_exec($ch);

    if (curl_errno($ch)) {
        writeLog("ERROR cURL al llamar al servicio: " . curl_error($ch));
        curl_close($ch);
        return false;
    }

    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    writeLog("Respuesta del servicio (HTTP {$httpCode}): " . substr($response, 0, 500));

    if ($httpCode !== 200) {
        $error = json_decode($response, true);
        writeLog("ERROR del servicio: " . ($error['detail'] ?? $response));
        return false;
    }

    $data = json_decode($response, true);
    if (!$data || empty($data['media'])) {
        writeLog("ERROR: Respuesta del servicio no contiene medios.");
        return false;
    }

    return $data['media'];
}

// Función unificada para manejar enlaces de Instagram y Twitter/X
function handleMediaLink($config, $chatId, $url, $platform) {
    $platformLabel = $platform === 'instagram' ? 'Instagram' : 'X';
    sendTelegramMessage($config['token'], $chatId, "🔄 Procesando tu enlace de {$platformLabel}...");

    $mediaItems = getMediaFromYtDlpService($url, $config);

    if (!$mediaItems) {
        sendTelegramMessage(
            $config['token'],
            $chatId,
            "❌ No pude obtener el contenido de {$platformLabel}.\n\n" .
            "Posibles causas:\n" .
            "• El post es privado.\n" .
            "• El servicio de extracción no está disponible.\n" .
            "Intenta de nuevo o revisa el log.",
            'HTML'
        );
        return;
    }

    $total_items = count($mediaItems);
    $count = 0;
    foreach ($mediaItems as $item) {
        $count++;
        $caption = $total_items > 1 ? "📸 Elemento {$count} de {$total_items}\n{$url}" : $url;

        // Descargar a través del proxy de Railway (misma IP que obtuvo la URL del CDN)
        $file = downloadFile(buildProxyUrl($item['url'], $config));
        if ($file) {
            uploadToTelegram($config['token'], $chatId, $item['type'], $file['path'], $file['content_type'], $caption);
            @unlink($file['path']);
        } else {
            writeLog("ERROR: no se pudo descargar el item {$count}: " . $item['url']);
        }
        sleep(1); // Evitar límites de Telegram
    }

    sendTelegramMessage($config['token'], $chatId, "✅ ¡Listo! Enviados {$count} archivo(s).");
}

// Función principal para procesar el webhook
function processWebhook() {
    writeLog("Iniciando procesamiento del webhook.");
    $config = loadConfig();
    if (!$config) {
        http_response_code(500);
        exit('Error de configuración');
    }

    $input = file_get_contents('php://input');
    $update = json_decode($input, true);
    writeLog("Webhook input: " . $input);

    if (!$update || !isset($update['message'])) {
        http_response_code(200);
        exit('OK');
    }

    $message = $update['message'];
    $chatId  = $message['chat']['id'];
    $text    = $message['text'] ?? '';
    writeLog("Mensaje recibido (chatId: {$chatId}, texto: {$text})");

    // Responder a Telegram inmediatamente para no hacer timeout
    http_response_code(200);
    header('Content-Type: text/plain');
    header('Content-Length: 2');
    echo 'OK';
    if (function_exists('fastcgi_finish_request')) {
        fastcgi_finish_request();
    } else {
        if (ob_get_level() > 0) ob_end_flush();
        flush();
    }
    ignore_user_abort(true);
    set_time_limit(300); // 5 minutos para descargar y subir archivos

    if ($text === '/start') {
        sendTelegramMessage(
            $config['token'],
            $chatId,
            "¡Hola! 👋\n\nEnvíame un enlace de Instagram o Twitter/X y te descargaré las imágenes y videos del post."
        );
        return;
    }

    $linkType = getLinkType($text);
    if (!$linkType) {
        sendTelegramMessage(
            $config['token'],
            $chatId,
            "Por favor, envía un enlace válido de Instagram o Twitter/X."
        );
        return;
    }

    handleMediaLink($config, $chatId, $text, $linkType);
}

// Ejecutar el procesamiento del webhook
processWebhook();
?>
