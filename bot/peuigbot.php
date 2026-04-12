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

// Funciones para enviar mensajes, fotos y videos a Telegram
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

function sendTelegramPhoto($token, $chatId, $photoUrl, $caption = '') {
    $url = "https://api.telegram.org/bot{$token}/sendPhoto";
    $data = ['chat_id' => $chatId, 'photo' => $photoUrl, 'caption' => $caption];
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        writeLog("ERROR al enviar foto a Telegram: " . curl_error($ch));
    } else {
        writeLog("Respuesta de Telegram (foto): " . $response);
    }
    curl_close($ch);
    return $response;
}

function sendTelegramVideo($token, $chatId, $videoUrl, $caption = '') {
    $url = "https://api.telegram.org/bot{$token}/sendVideo";
    $data = ['chat_id' => $chatId, 'video' => $videoUrl, 'caption' => $caption];
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_POST, 1);
    curl_setopt($ch, CURLOPT_POSTFIELDS, http_build_query($data));
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    $response = curl_exec($ch);
    if (curl_errno($ch)) {
        writeLog("ERROR al enviar video a Telegram: " . curl_error($ch));
    } else {
        writeLog("Respuesta de Telegram (video): " . $response);
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
        if ($item['type'] === 'image') {
            sendTelegramPhoto($config['token'], $chatId, $item['url'], $caption);
        } elseif ($item['type'] === 'video') {
            sendTelegramVideo($config['token'], $chatId, $item['url'], $caption);
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
    $chatId = $message['chat']['id'];
    $text = $message['text'] ?? '';
    writeLog("Mensaje recibido (chatId: {$chatId}, texto: {$text})");

    if ($text === '/start') {
        sendTelegramMessage(
            $config['token'],
            $chatId,
            "¡Hola! 👋\n\nEnvíame un enlace de Instagram o Twitter/X y te descargaré las imágenes y videos del post."
        );
        http_response_code(200);
        exit('OK');
    }

    $linkType = getLinkType($text);
    if (!$linkType) {
        sendTelegramMessage(
            $config['token'],
            $chatId,
            "Por favor, envía un enlace válido de Instagram o Twitter/X."
        );
        http_response_code(200);
        exit('OK');
    }

    handleMediaLink($config, $chatId, $text, $linkType);

    http_response_code(200);
    exit('OK');
}

// Ejecutar el procesamiento del webhook
processWebhook();
?>
