// [fork-only] Conversations screen → "Show discarded" chip.
//
// "Only my conversations" (owner_only_toggle.dart) hides recordings where the
// owner's voice is not heard. Upstream lets the user see them again only from
// Settings → Conversation display. This chip puts the same switch
// (ConversationProvider.toggleDiscardConversations) on the list itself.
import 'package:flutter/material.dart';

import 'package:provider/provider.dart';

import 'package:omi/providers/conversation_provider.dart';
import 'package:omi/ui/ui.dart';

class ShowDiscardedChip extends StatelessWidget {
  const ShowDiscardedChip({super.key});

  @override
  Widget build(BuildContext context) {
    final showing = context.select<ConversationProvider, bool>((p) => p.showDiscardedConversations);
    return Padding(
      padding: const EdgeInsets.fromLTRB(OmiSpacing.md, OmiSpacing.xs, OmiSpacing.md, 0),
      child: Align(
        alignment: Alignment.centerLeft,
        child: OmiFilterChip(
          key: const ValueKey('conversations_show_discarded'),
          label: 'Show discarded',
          icon: Icons.visibility_off_outlined,
          selected: showing,
          semanticsLabel: showing ? 'Hide discarded conversations' : 'Show discarded conversations',
          onSelected: () => context.read<ConversationProvider>().toggleDiscardConversations(),
        ),
      ),
    );
  }
}
