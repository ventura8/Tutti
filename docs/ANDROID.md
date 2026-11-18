# Android Support Plan

The Tutti project already has a PWA that works on Android. This task focuses on enhancing the PWA to provide a "native-like" experience on Android phones.

## Goals

- **Google Sign-in**: Ensure it works flawlessly on Android phones.
- **Push Notifications**: Enable notifications for "Needs" and other important updates.
- **Mobile-friendly UI**:
    - Chat interface optimized for phones.
    - "Needs" management with clear approval buttons.
    - Job info (ETA, usage/costs) mobile view.
    - Device and machine status mobile view.

## Implementation Plan

1.  **Push Notifications**:
    - Update `sw.js` to handle `push` events.
    - Implement a way for the server to trigger push notifications to subscribed clients.
    - Update the PWA UI to ask for notification permission.

2.  **Mobile UI Enhancements**:
    - Add CSS media queries for mobile-friendly layouts (`tutti/web/app.css`).
    - Update `needs.js` and `chat.js` to include easy-to-use action buttons (e.g., approval buttons).

3.  **Testing**:
    - Test the PWA behavior on a real Android device (if accessible) or via browser mobile emulation.
