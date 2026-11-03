import 'package:flutter_test/flutter_test.dart';

import 'package:zakaz/main.dart';

void main() {
  test('Ilova saytni skaner rejimida ochadi', () {
    expect(appUri.path, '/app');
    expect(appUri.queryParameters['app'], '1');
    expect(appUri.scheme, 'https');
  });
}
