// [fork-only] A Sync tap re-offers failed and server-refused recordings.
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:omi/backend/preferences.dart';
import 'package:omi/backend/schema/bt_device/bt_device.dart';
import 'package:omi/backend/schema/conversation.dart';
import 'package:omi/services/wals/local_wal_sync.dart';
import 'package:omi/services/wals/wal.dart';
import 'package:omi/services/wals/wal_interfaces.dart';

class _Listener implements IWalSyncListener {
  @override
  void onWalUpdated() {}

  @override
  void onWalSynced(Wal wal, {ServerConversation? conversation}) {}
}

Wal _wal(int start, WalStatus status, {int retryCount = 0, WalStorage storage = WalStorage.disk}) => Wal(
      timerStart: start,
      codec: BleAudioCodec.opus,
      seconds: 60,
      storage: storage,
      status: status,
      retryCount: retryCount,
      lastRetryAt: retryCount > 0 ? 99 : 0,
    );

void main() {
  setUp(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({});
    await SharedPreferencesUtil.init();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
      const MethodChannel('plugins.flutter.io/path_provider'),
      (MethodCall call) async {
        if (call.method == 'getApplicationDocumentsDirectory') return Directory.systemTemp.path;
        return null;
      },
    );
  });

  test('re-arms exhausted and rejected disk recordings, leaves the rest', () async {
    var persisted = <Wal>[];
    final local = LocalWalSyncImpl(
      _Listener(),
      persistWals: (wals) async => persisted = List<Wal>.from(wals),
      loadWals: () async => <Wal>[],
    );
    final exhausted = _wal(100, WalStatus.miss, retryCount: walMaxAutoRetries);
    final rejected = _wal(200, WalStatus.uploadRejected, retryCount: walMaxAutoRetries);
    final budgeted = _wal(300, WalStatus.miss, retryCount: 2);
    final corrupted = _wal(400, WalStatus.corrupted);
    final tooOld = _wal(500, WalStatus.outsideRecoveryWindow);
    final rejectedOnSdCard = _wal(600, WalStatus.uploadRejected, storage: WalStorage.sdcard);
    local.testWals = [exhausted, rejected, budgeted, corrupted, tooOld, rejectedOnSdCard];

    expect(await local.rearmForUserSync(), 2);

    for (final wal in [exhausted, rejected]) {
      expect(wal.status, WalStatus.miss);
      expect(wal.retryCount, 0);
      expect(wal.lastRetryAt, 0);
    }
    expect(budgeted.retryCount, 2);
    expect(corrupted.status, WalStatus.corrupted);
    expect(tooOld.status, WalStatus.outsideRecoveryWindow);
    expect(rejectedOnSdCard.status, WalStatus.uploadRejected);
    expect(persisted.map((wal) => wal.id), contains(rejected.id));
  });
}
