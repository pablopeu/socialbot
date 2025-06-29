<?php
// combined_bot.php - Bot de Telegram para descargar contenido de Instagram y Twitter/X

// Definir constantes y configuración
define('LOG_FILE', __DIR__ . '/combined_bot.log');
define('CFG_FILE', __DIR__ . '/config.txt');
define('DEBUG_MODE', true); // Ajustar a false en producción
define('COOKIE_JAR', __DIR__ . '/instagram_cookies.txt');
define('THIRD_PARTY_API_BASE_URL', 'https://instagram-looter2.p.rapidapi.com');
define('X_RAPIDAPI_HOST', 'instagram-looter2.p.rapidapi.com');

// Función para registrar mensajes en el archivo de log
function writeLog($message) {
    $timestamp = date('Y-m-d H:i:s');
    file_put_contents(LOG_FILE, "[{$timestamp}] {$message}\n", FILE_APPEND);
}

// Función para leer configuración
function loadConfig() {
    writeLog("Cargando configuración desde " . CFG_FILE);
    if (!file_exists(CFG_FILE)) {
        writeLog('ERROR: Archivo ' . CFG_FILE . ' no encontrado.');
        error_log('Archivo ' . CFG_FILE . ' no encontrado');
        return false;
    }
    
    $config = [];
    $lines = file(CFG_FILE, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    
    foreach ($lines as $line) {
        $line = trim($line);
        if (strpos($line, '#') === 0 || empty($line)) {
            continue;
        }
        if (strpos($line, ':') !== false) {
            list($key, $value) = explode(':', $line, 2);
            $config[trim($key)] = trim($value);
        }
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

// Función para extraer el shortcode de Instagram
function extractInstagramShortcode($url) {
    writeLog("Intentando extraer shortcode de la URL: {$url}");
    $url = strtok($url, '?');
    $patterns = [
        '/instagram\.com\/p\/([A-Za-z0-9_-]+)/',
        '/instagram\.com\/reel\/([A-Za-z0-9_-]+)/',
        '/instagr\.am\/p\/([A-Za-z0-9_-]+)/',
        '/instagram\.com\/tv\/([A-Za-z0-9_-]+)/'
    ];
    foreach ($patterns as $pattern) {
        if (preg_match($pattern, $url, $matches)) {
            writeLog("Shortcode extraído: {$matches[1]}");
            return $matches[1];
        }
    }
    writeLog("No se pudo extraer el shortcode de la URL.");
    return false;
}

// Función para enviar una solicitud HTTP
function sendHttpRequest($url, $extraHeaders = [], $timeout = 60) {
    writeLog("Realizando petición HTTP a: {$url}");
    $ch = curl_init();
    curl_setopt($ch, CURLOPT_URL, $url);
    curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
    curl_setopt($ch, CURLOPT_HEADER, true);
    curl_setopt($ch, CURLOPT_FOLLOWLOCATION, true);
    curl_setopt($ch, CURLOPT_TIMEOUT, $timeout);
    curl_setopt($ch, CURLOPT_ENCODING, "");
    curl_setopt($ch, CURLOPT_COOKIEJAR, COOKIE_JAR);
    curl_setopt($ch, CURLOPT_COOKIEFILE, COOKIE_JAR);

    $headers = [
        'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36',
        'Accept: */*',
        'Accept-Language: es-ES,es;q=0.9,en;q=0.8',
        'Connection: keep-alive',
    ];

    foreach ($extraHeaders as $extraHeader) {
        list($name, $value) = explode(':', $extraHeader, 2);
        $name = trim($name);
        $found = false;
        foreach ($headers as $key => $header) {
            if (stripos($header, $name . ':') === 0) {
                $headers[$key] = $extraHeader;
                $found = true;
                break;
            }
        }
        if (!$found) {
            $headers[] = $extraHeader;
        }
    }
    
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
    $response = curl_exec($ch);
    
    if (curl_errno($ch)) {
        writeLog("ERROR: Falló la petición HTTP (cURL). Detalles: " . curl_error($ch));
        curl_close($ch);
        return false;
    }

    $header_size = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
    $response_body = substr($response, $header_size);
    $http_status_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    
    writeLog("Respuesta HTTP: Código {$http_status_code}, Cuerpo (primeros 500 chars): " . substr($response_body, 0, 500) . "...");
    if (DEBUG_MODE) {
        writeLog("Cuerpo completo de la respuesta HTTP: " . $response_body);
    }
    
    curl_close($ch);
    return ['body' => $response_body, 'status_code' => $http_status_code];
}

// Función para obtener datos de Instagram usando la API de terceros
function getInstagramDataFromThirdPartyAPI($shortcode, $config) {
    if (!isset($config['third_party_api_key'])) {
        writeLog("ERROR: third_party_api_key no encontrada en config.txt.");
        return false;
    }

    $instagram_url = "https://www.instagram.com/p/{$shortcode}/";
    $api_url = THIRD_PARTY_API_BASE_URL . '/post?url=' . urlencode($instagram_url);
    $extraHeaders = [
        'X-RapidAPI-Key: ' . $config['third_party_api_key'],
        'X-RapidAPI-Host: ' . X_RAPIDAPI_HOST,
        'Content-Type: application/json',
    ];

    $response = sendHttpRequest($api_url, $extraHeaders);
    if (!$response || $response['status_code'] !== 200) {
        writeLog("ERROR: La petición a la API de terceros falló. Código: " . ($response['status_code'] ?? 'N/A'));
        return false;
    }

    $data = json_decode($response['body'], true);
    if (!$data) {
        writeLog("ERROR: Respuesta de la API no es JSON válido.");
        return false;
    }

    $mediaItems = [];
    if (isset($data['__typename']) && ($data['__typename'] === 'GraphImage' || $data['__typename'] === 'GraphVideo')) {
        $item_type = ($data['__typename'] === 'GraphVideo') ? 'video' : 'image';
        $item_url = ($item_type === 'video' && isset($data['video_url'])) ? $data['video_url'] : $data['display_url'];
        if (filter_var($item_url, FILTER_VALIDATE_URL)) {
            $mediaItems[] = ['type' => $item_type, 'url' => $item_url];
        }
    } elseif (isset($data['__typename']) && $data['__typename'] === 'GraphSidecar' && isset($data['edge_sidecar_to_children']['edges'])) {
        foreach ($data['edge_sidecar_to_children']['edges'] as $edge) {
            $node = $edge['node'];
            $item_type = ($node['__typename'] === 'XDTGraphVideo') ? 'video' : 'image';
            $item_url = ($item_type === 'video' && isset($node['video_url'])) ? $node['video_url'] : $node['display_url'];
            if (filter_var($item_url, FILTER_VALIDATE_URL)) {
                $mediaItems[] = ['type' => $item_type, 'url' => $item_url];
            }
        }
    }

    if (!empty($mediaItems)) {
        return ['type' => count($mediaItems) > 1 ? 'carousel' : 'single', 'items' => $mediaItems];
    }
    return false;
}

// Lógica para manejar enlaces de Instagram
function handleInstagramLink($config, $chatId, $url) {
    $shortcode = extractInstagramShortcode($url);
    if (!$shortcode) {
        sendTelegramMessage($config['token'], $chatId, "No pude extraer el código del post de Instagram.");
        return;
    }

    sendTelegramMessage($config['token'], $chatId, "🔄 Procesando tu enlace de Instagram...");
    $instagramData = getInstagramDataFromThirdPartyAPI($shortcode, $config);

    if (!$instagramData || empty($instagramData['items'])) {
        sendTelegramMessage(
            $config['token'], 
            $chatId, 
            "❌ No pude obtener el contenido de Instagram.\n\n" .
            "Posibles causas:\n" .
            "• El post es privado.\n" .
            "• Problemas con la API de terceros.\n" .
            "• Límite de API alcanzado.\n" .
            "Intenta de nuevo o revisa el log.",
            'HTML'
        );
        return;
    }

    $total_items = count($instagramData['items']);
    $count = 0;
    foreach ($instagramData['items'] as $item) {
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

// Lógica para manejar enlaces de Twitter/X sin reintentos
function handleTwitterLink($config, $chatId, $url) {
    if (!preg_match('#/status/(\d+)#', $url, $match)) {
        sendTelegramMessage($config['token'], $chatId, 'Envíame un enlace válido de X/Twitter.');
        return;
    }
    $tweetId = $match[1];

    // Enviar mensaje inicial
    sendTelegramMessage($config['token'], $chatId, "🔄 Procesando tu enlace de X...");

    $apiUrl = "https://api.twitter.com/2/tweets/{$tweetId}?expansions=attachments.media_keys&tweet.fields=attachments&media.fields=type,url,preview_image_url,variants";
    $ch = curl_init($apiUrl);
    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HTTPHEADER     => [
            "Authorization: Bearer {$config['TWITTER_BEARER']}",
            'User-Agent: TelegramBot/1.0'
        ],
        CURLOPT_TIMEOUT        => 10
    ]);
    $resp = curl_exec($ch);
    if (curl_errno($ch)) {
        writeLog("cURL error en Twitter API: " . curl_error($ch));
        sendTelegramMessage($config['token'], $chatId, 'Error al acceder a la API de Twitter.');
        curl_close($ch);
        return;
    }
    $httpCode = curl_getinfo($ch, CURLINFO_HTTP_CODE);
    curl_close($ch);

    $data = json_decode($resp, true);
    if ($httpCode === 429) {
        writeLog("Límite alcanzado (429). Respuesta: " . json_encode($data));
        sendTelegramMessage($config['token'], $chatId, 'Límite de la API de Twitter alcanzado. Intenta de nuevo más tarde.');
        return;
    } elseif ($httpCode !== 200) {
        writeLog("Error HTTP de Twitter API: {$httpCode}, Respuesta: " . json_encode($data));
        sendTelegramMessage($config['token'], $chatId, 'Error al procesar la solicitud de Twitter.');
        return;
    }

    if (json_last_error() !== JSON_ERROR_NONE) {
        writeLog("Error al decodificar JSON de Twitter API: " . json_last_error_msg());
        sendTelegramMessage($config['token'], $chatId, 'Error al procesar la respuesta de Twitter.');
        return;
    }

    $mediaUrls = [];
    if (!empty($data['includes']['media'])) {
        foreach ($data['includes']['media'] as $media) {
            if ($media['type'] === 'photo' && !empty($media['url'])) {
                $mediaUrls[] = ['type' => 'photo', 'url' => $media['url']];
            } elseif ($media['type'] === 'video') {
                if (!empty($media['preview_image_url'])) {
                    $mediaUrls[] = ['type' => 'photo', 'url' => $media['preview_image_url']];
                }
                if (!empty($media['variants'])) {
                    foreach ($media['variants'] as $variant) {
                        if ($variant['content_type'] === 'video/mp4' && !empty($variant['url'])) {
                            $mediaUrls[] = ['type' => 'video', 'url' => $variant['url']];
                            break;
                        }
                    }
                }
            }
        }
    }

    if (empty($mediaUrls)) {
        sendTelegramMessage($config['token'], $chatId, 'No se encontraron medios en el post.');
        return;
    }

    $total_items = count($mediaUrls);
    $count = 0;
    foreach ($mediaUrls as $media) {
        $count++;
        $caption = $total_items > 1 ? "📸 Elemento {$count} de {$total_items}" : "";
        if ($media['type'] === 'photo') {
            sendTelegramPhoto($config['token'], $chatId, $media['url'], $caption);
        } elseif ($media['type'] === 'video') {
            sendTelegramVideo($config['token'], $chatId, $media['url'], $caption);
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

    if ($linkType === 'instagram') {
        handleInstagramLink($config, $chatId, $text);
    } elseif ($linkType === 'twitter') {
        handleTwitterLink($config, $chatId, $text);
    }
    
    http_response_code(200);
    exit('OK');
}

// Ejecutar el procesamiento del webhook
processWebhook();
?>