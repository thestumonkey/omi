// [fork-only] Settings → Recording & Transcription → "Only my conversations".
//
// A server-wide switch on the self-hosted backend (GET/POST
// v1/selfhosted/capture). When on, a recording where the owner's voice is not
// heard (TV, radio, other people) gets no summary, tasks or memories and is
// hidden under Show discarded. The row hides itself when the server does not
// offer the setting (an upstream backend).
import 'dart:convert';

import 'package:flutter/material.dart';

import 'package:font_awesome_flutter/font_awesome_flutter.dart';

import 'package:omi/backend/http/shared.dart';
import 'package:omi/env/env.dart';
import 'package:omi/ui/ui.dart';

Future<bool?> _ownerOnlyCall({bool? set}) async {
  final response = await makeApiCall(
    url: '${Env.apiBaseUrl}v1/selfhosted/capture',
    headers: {},
    method: set == null ? 'GET' : 'POST',
    body: set == null ? '' : jsonEncode({'owner_only': set}),
  );
  if (response == null || response.statusCode != 200) return null;
  final value = (jsonDecode(response.body) as Map<String, dynamic>)['owner_only'];
  return value is bool ? value : null;
}

class OwnerOnlyToggle extends StatefulWidget {
  const OwnerOnlyToggle({super.key});

  @override
  State<OwnerOnlyToggle> createState() => _OwnerOnlyToggleState();
}

class _OwnerOnlyToggleState extends State<OwnerOnlyToggle> {
  bool? _value;
  bool _saving = false;

  @override
  void initState() {
    super.initState();
    _ownerOnlyCall().then((value) {
      if (mounted) setState(() => _value = value);
    });
  }

  Future<void> _set(bool value) async {
    setState(() => _saving = true);
    final saved = await _ownerOnlyCall(set: value);
    if (!mounted) return;
    setState(() {
      _saving = false;
      if (saved != null) _value = saved;
    });
    if (saved == null) OmiFeedback.error(context, 'Could not change this setting. Try again.');
  }

  @override
  Widget build(BuildContext context) {
    final value = _value;
    if (value == null) return const SizedBox.shrink();
    return OmiSettingsRow.toggle(
      key: const ValueKey('settings_row_ownerOnly'),
      leading: const FaIcon(FontAwesomeIcons.userCheck),
      title: 'Only my conversations',
      subtitle: value
          ? 'Recordings without your voice (TV, radio, other people) get no summary, tasks or memories. '
              'They go to Show discarded. Needs your Voice Profile.'
          : 'Every recording is summarized, including TV and other people.',
      value: value,
      onChanged: _saving ? null : _set,
    );
  }
}
