// Ona tili · Skaner — Android ilova. Botning Mini App saytini WebView ichida "skaner rejimida" ochadi.
//
// Telegram ichida bo'lmagani uchun kirish bot orqali: sayt "Telegram orqali kirish" tugmasini ko'rsatadi,
// t.me/<bot>?start=app_<token> ochiladi, botda "Start" bosilgach ilova o'z sessiyasini oladi.
// Ilovada faqat o'qituvchining ochiq (titulli) testlari va jonli skaner bor; saqlangan natija
// bot chatiga ham yuboriladi.
//
// Ilova:
//  - user-agent'ga "OnatiliApp" qo'shadi (sayt shu bo'yicha ilova ekanini biladi);
//  - t.me va boshqa tashqi havolalarni Telegram/brauzerda ochadi;
//  - Android "orqaga" tugmasini saytdagi "orqaga" ga ulaydi;
//  - internet yo'q bo'lsa, qayta urinish oynasini ko'rsatadi;
//  - jonli skaner uchun kameraga ruxsat beradi.
//
// Sayt manzili yig'ishda beriladi:
//   flutter build apk --release --dart-define=APP_URL=https://<bot>.onrender.com/

import 'dart:async';
import 'dart:io' show Platform;

import 'package:app_links/app_links.dart';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_inappwebview/flutter_inappwebview.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:url_launcher/url_launcher.dart';

const _configuredUrl = String.fromEnvironment(
  'APP_URL',
  defaultValue: 'https://zakaz-9s4l.onrender.com/',
);
final appUri = Uri.parse(
  _configuredUrl,
).replace(path: '/app', queryParameters: {'app': '1'});

// onatiliskaner://open?t=12&login=abc → https://<sayt>/app?app=1&t=12&login=abc (shu test skaneri, avtomatik kirish)
Uri pageFor(Uri? link) {
  if (link == null || link.scheme != 'onatiliskaner') return appUri;
  final params = <String, String>{'app': '1'};
  for (final key in ['t', 'login']) {
    final value = link.queryParameters[key];
    if (value != null && RegExp(r'^[0-9a-f]{1,64}$').hasMatch(value)) {
      params[key] = value;
    }
  }
  return appUri.replace(queryParameters: params);
}

const _green = Color(0xFF1D6F42);
const _background = Color(0xFFF4FAF6);

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const ScannerApp());
}

class ScannerApp extends StatelessWidget {
  const ScannerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Ona tili · Skaner',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorSchemeSeed: _green,
        scaffoldBackgroundColor: _background,
      ),
      home: const WebShell(),
    );
  }
}

class WebShell extends StatefulWidget {
  const WebShell({super.key});

  @override
  State<WebShell> createState() => _WebShellState();
}

class _WebShellState extends State<WebShell> {
  InAppWebViewController? _controller;
  double _progress = 0;
  bool _offline = false;
  bool _loaded = false;
  Uri _start = appUri;
  bool _ready = false;
  final _links = AppLinks();
  StreamSubscription<Uri>? _linkSub;

  @override
  void initState() {
    super.initState();
    _initLinks();
  }

  Future<void> _initLinks() async {
    try {
      _start = pageFor(await _links.getInitialLink());
    } catch (_) {}
    if (mounted) setState(() => _ready = true);
    // Ilova ochiq turganda botdan yangi havola keldi — shu sahifani ochamiz
    _linkSub = _links.uriLinkStream.listen((link) {
      final page = pageFor(link);
      if (page != appUri) {
        _controller?.loadUrl(urlRequest: URLRequest(url: WebUri.uri(page)));
      }
    });
  }

  @override
  void dispose() {
    _linkSub?.cancel();
    super.dispose();
  }

  bool _isOwnSite(WebUri uri) =>
      uri.host == appUri.host &&
      (uri.scheme == 'https' || uri.scheme == 'http');

  Future<void> _openExternal(WebUri uri) async {
    final target = Uri.parse(uri.toString());
    var opened = false;
    try {
      opened = await launchUrl(target, mode: LaunchMode.externalApplication);
    } catch (_) {
      opened = false;
    }
    if (opened) return;
    // Telegram o'rnatilmagan bo'lsa: tg://resolve?domain=x -> https://t.me/x brauzerda
    var fallback = target;
    if (target.scheme == 'tg') {
      final domain = target.queryParameters['domain'];
      if (domain == null) return;
      final start = target.queryParameters['start'];
      fallback = Uri.https(
        't.me',
        '/$domain',
        start == null ? null : {'start': start},
      );
    }
    try {
      await launchUrl(fallback, mode: LaunchMode.platformDefault);
    } catch (_) {
      // Ochib bo'lmadi — sahifada qolamiz
    }
  }

  Future<void> _handleBack() async {
    final handled = await _controller?.evaluateJavascript(
      source: 'window.appBack ? window.appBack() : false',
    );
    if (handled == true) return;
    await SystemNavigator.pop();
  }

  void _retry() {
    setState(() => _offline = false);
    _controller?.reload();
  }

  @override
  Widget build(BuildContext context) {
    return PopScope(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _handleBack();
      },
      child: Scaffold(
        body: SafeArea(
          child: Stack(
            children: [
              if (_ready)
                InAppWebView(
                  initialUrlRequest: URLRequest(url: WebUri.uri(_start)),
                  initialSettings: InAppWebViewSettings(
                    applicationNameForUserAgent: 'OnatiliApp/1.0',
                    javaScriptEnabled: true,
                    domStorageEnabled: true,
                    useShouldOverrideUrlLoading: true,
                    supportMultipleWindows: true,
                    javaScriptCanOpenWindowsAutomatically: true,
                    mediaPlaybackRequiresUserGesture: false,
                    allowsInlineMediaPlayback: true,
                    supportZoom: false,
                  ),
                  onWebViewCreated: (controller) => _controller = controller,
                  // Faqat o'z saytimiz ilova ichida; qolgani (t.me — kirish, kanallar) tashqarida
                  shouldOverrideUrlLoading: (controller, action) async {
                    final uri = action.request.url;
                    if (uri == null) return NavigationActionPolicy.ALLOW;
                    final internal =
                        uri.scheme == 'about' ||
                        uri.scheme == 'blob' ||
                        uri.scheme == 'data';
                    if (_isOwnSite(uri) || internal) {
                      return NavigationActionPolicy.ALLOW;
                    }
                    await _openExternal(uri);
                    return NavigationActionPolicy.CANCEL;
                  },
                  onCreateWindow: (controller, action) async {
                    final uri = action.request.url;
                    if (uri != null) await _openExternal(uri);
                    return false;
                  },
                  // Jonli skaner kamerani so'raydi — faqat o'z saytimizga beriladi
                  onPermissionRequest: (controller, request) async {
                    final wantsCamera = request.resources.contains(
                      PermissionResourceType.CAMERA,
                    );
                    final ownSite = request.origin.host == appUri.host;
                    final granted =
                        wantsCamera &&
                        ownSite &&
                        (Platform.isIOS ||
                            (await Permission.camera.request()).isGranted);
                    return PermissionResponse(
                      resources: request.resources,
                      action: granted
                          ? PermissionResponseAction.GRANT
                          : PermissionResponseAction.DENY,
                    );
                  },
                  onProgressChanged: (controller, progress) =>
                      setState(() => _progress = progress / 100),
                  onPageCommitVisible: (controller, url) => setState(() {
                    _progress = 1;
                    _loaded = true;
                  }),
                  onLoadStop: (controller, url) => setState(() {
                    _progress = 1;
                    _loaded = true;
                  }),
                  onReceivedError: (controller, request, error) {
                    if (request.isForMainFrame ?? true) {
                      setState(() => _offline = true);
                    }
                  },
                ),
              if (_progress < 1 && _loaded)
                LinearProgressIndicator(
                  value: _progress,
                  minHeight: 3,
                  color: _green,
                ),
              IgnorePointer(
                ignoring: _loaded,
                child: AnimatedOpacity(
                  opacity: _loaded ? 0 : 1,
                  duration: const Duration(milliseconds: 300),
                  child: const _SplashView(),
                ),
              ),
              if (_offline) _OfflineView(onRetry: _retry),
            ],
          ),
        ),
      ),
    );
  }
}

class _SplashView extends StatelessWidget {
  const _SplashView();

  @override
  Widget build(BuildContext context) {
    return Container(
      color: _green,
      alignment: Alignment.center,
      child: const Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Text('📷', style: TextStyle(fontSize: 64)),
          SizedBox(height: 12),
          Text(
            'Ona tili · Skaner',
            style: TextStyle(
              color: Colors.white,
              fontSize: 28,
              fontWeight: FontWeight.w800,
            ),
          ),
          SizedBox(height: 8),
          Text(
            'Titullarni kamerada tekshirish',
            style: TextStyle(color: Colors.white70, fontSize: 16),
          ),
          SizedBox(height: 28),
          SizedBox(
            width: 28,
            height: 28,
            child: CircularProgressIndicator(
              color: Colors.white,
              strokeWidth: 3,
            ),
          ),
        ],
      ),
    );
  }
}

class _OfflineView extends StatelessWidget {
  const _OfflineView({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return Container(
      color: _background,
      padding: const EdgeInsets.all(32),
      alignment: Alignment.center,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Text('📡', style: TextStyle(fontSize: 56)),
          const SizedBox(height: 16),
          const Text(
            "Internet bilan aloqa yo'q",
            style: TextStyle(
              fontSize: 20,
              fontWeight: FontWeight.w800,
              color: _green,
            ),
          ),
          const SizedBox(height: 8),
          const Text(
            "Internetni tekshirib, qayta urinib ko'ring.",
            textAlign: TextAlign.center,
            style: TextStyle(color: Colors.black54),
          ),
          const SizedBox(height: 24),
          FilledButton(
            onPressed: onRetry,
            style: FilledButton.styleFrom(
              backgroundColor: _green,
              minimumSize: const Size(200, 48),
            ),
            child: const Text('Qayta urinish'),
          ),
        ],
      ),
    );
  }
}
