/*
 * wayland_input.c — Wayland-native input injection via Mutter RemoteDesktop + libei
 * ================================================================================
 *
 * 原理：
 *   通过 DBus 调用 org.gnome.Mutter.RemoteDesktop.CreateSession 创建远程桌面会话，
 *   然后调用 ConnectToEIS 获取 EIS (Emulated Input Server) 的文件描述符，
 *   最后用 libei 库通过这个 fd 发送 Wayland 原生的鼠标/键盘事件。
 *
 *   这样生成的事件与真实硬件输入无法区分，所有 Wayland 原生应用（Chrome、GNOME 等）
 *   都会正确响应。
 *
 * ---- 编译依赖 ----
 *   Ubuntu/Debian:
 *     sudo apt-get install libei-dev libglib2.0-dev libpixman-1-dev
 *
 *   Fedora:
 *     sudo dnf install libei-devel glib2-devel pixman-devel
 *
 * ---- 编译命令 ----
 *   gcc -O2 -o wayland_input wayland_input.c $(pkg-config --cflags --libs glib-2.0 gio-2.0 libei-1.0)
 *
 *   编译产物 wayland_input 放到本目录下，computer_use_mcp.py 会自动发现并启动它。
 *
 * ---- 独立测试 ----
 *   # 启动守护进程（静默模式）
 *   ./wayland_input -q
 *
 *   # 然后通过 stdin 发送命令：
 *   echo "move_abs 500 300" | ./wayland_input -q
 *   echo "click 1" | ./wayland_input -q
 *   echo "type Hello" | ./wayland_input -q
 *
 * ---- Python 集成 ----
 *   computer_use_mcp.py 内置了完整的 Python 封装（_wi_* 函数），无需额外文件。
 *   在 Wayland 桌面环境下启动 MCP server 时会自动检测并使用。
 *
 * ---- stdin 命令格式 (每行一个，空格分隔) ----
 *   move_abs X Y       - 绝对坐标移动 (逻辑像素)
 *   move_rel DX DY     - 相对移动
 *   click BUTTON       - 点击 (1=左键, 2=中键, 3=右键)
 *   press BUTTON       - 按下按键不放
 *   release BUTTON     - 释放按键
 *   key KEYCODE STATE  - 键盘事件 (STATE: 1=按下, 0=释放, 键码为 Linux evdev keycode)
 *   type TEXT          - 输入字符串
 *   quit               - 关闭守护进程
 *
 * ---- stdout 输出格式 (每行一个) ----
 *   OK                 - 命令成功
 *   ERROR message      - 错误
 *   READY W H          - 守护进程就绪，屏幕尺寸 W x H
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <poll.h>
#include <gio/gio.h>
#include <libei.h>

// --- Globals ---
static struct ei *ei_ctx = NULL;
static struct ei_device *ei_pointer = NULL;
static struct ei_device *ei_keyboard = NULL;
static int screen_width = 0;
static int screen_height = 0;
static int ready = 0;
static int quiet = 0;

#define LOG(fmt, ...) do { if (!quiet) fprintf(stderr, fmt "\n", ##__VA_ARGS__); } while(0)

// --- helper: map simple chars to Linux evdev keycodes ---
static int char_to_keycode(char c) {
    switch (c) {
    case '`': case '~': return 41;
    case '1': case '!': return 2;
    case '2': case '@': return 3;
    case '3': case '#': return 4;
    case '4': case '$': return 5;
    case '5': case '%': return 6;
    case '6': case '^': return 7;
    case '7': case '&': return 8;
    case '8': case '*': return 9;
    case '9': case '(': return 10;
    case '0': case ')': return 11;
    case '-': case '_': return 12;
    case '=': case '+': return 13;
    case 'a': case 'A': return 30;
    case 'b': case 'B': return 48;
    case 'c': case 'C': return 46;
    case 'd': case 'D': return 32;
    case 'e': case 'E': return 18;
    case 'f': case 'F': return 33;
    case 'g': case 'G': return 34;
    case 'h': case 'H': return 35;
    case 'i': case 'I': return 23;
    case 'j': case 'J': return 36;
    case 'k': case 'K': return 37;
    case 'l': case 'L': return 38;
    case 'm': case 'M': return 50;
    case 'n': case 'N': return 49;
    case 'o': case 'O': return 24;
    case 'p': case 'P': return 25;
    case 'q': case 'Q': return 16;
    case 'r': case 'R': return 19;
    case 's': case 'S': return 31;
    case 't': case 'T': return 20;
    case 'u': case 'U': return 22;
    case 'v': case 'V': return 47;
    case 'w': case 'W': return 17;
    case 'x': case 'X': return 45;
    case 'y': case 'Y': return 21;
    case 'z': case 'Z': return 44;
    case '[': case '{': return 26;
    case ']': case '}': return 27;
    case '\\': case '|': return 43;
    case ';': case ':': return 39;
    case '\'': case '"': return 40;
    case ',': case '<': return 51;
    case '.': case '>': return 52;
    case '/': case '?': return 53;
    case ' ': return 57;
    case '\t': return 15;
    case '\n': case '\r': return 28;
    default: return -1;
    }
}

static int needs_shift(char c) {
    return (c >= 'A' && c <= 'Z') ||
           (strchr("~!@#$%^&*()_+{}|:\"<>?", c) != NULL);
}

// --- libei event handling ---
static void handle_libei_event(struct ei_event *event) {
    switch (ei_event_get_type(event)) {
    case EI_EVENT_CONNECT:
        LOG("[ei] Connected to EIS");
        break;

    case EI_EVENT_DISCONNECT:
        LOG("[ei] Disconnected from EIS");
        ready = 0;
        break;

    case EI_EVENT_SEAT_ADDED: {
        struct ei_seat *seat = ei_event_get_seat(event);
        LOG("[ei] Seat added, binding capabilities");
        ei_seat_bind_capabilities(seat,
            EI_DEVICE_CAP_POINTER,
            EI_DEVICE_CAP_POINTER_ABSOLUTE,
            EI_DEVICE_CAP_KEYBOARD,
            NULL);
        break;
    }

    case EI_EVENT_DEVICE_ADDED: {
        struct ei_device *dev = ei_event_get_device(event);
        LOG("[ei] Device added");
        // Prefer absolute pointer if available
        if (ei_device_has_capability(dev, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            ei_pointer = dev;
            LOG("[ei]   -> assigned as absolute pointer");
        } else if (ei_device_has_capability(dev, EI_DEVICE_CAP_POINTER) && !ei_pointer) {
            ei_pointer = dev;
            LOG("[ei]   -> assigned as relative pointer");
        }
        if (ei_device_has_capability(dev, EI_DEVICE_CAP_KEYBOARD) && !ei_keyboard) {
            ei_keyboard = dev;
            LOG("[ei]   -> assigned as keyboard");
        }
        break;
    }

    case EI_EVENT_DEVICE_RESUMED: {
        struct ei_device *dev = ei_event_get_device(event);
        ei_device_start_emulating(dev, 0);

        if (dev == ei_pointer && ei_device_has_capability(dev, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            struct ei_region *region = ei_device_get_region(dev, 0);
            if (region) {
                screen_width = (int)ei_region_get_width(region);
                screen_height = (int)ei_region_get_height(region);
                LOG("[ei] Absolute pointer resumed, screen: %dx%d",
                        screen_width, screen_height);
            }
            ready = 1;
        } else if (dev == ei_pointer) {
            LOG("[ei] Relative pointer resumed");
            // Only set ready if no absolute pointer is available
            ready = 1;
        }
        if (dev == ei_keyboard) {
            LOG("[ei] Keyboard resumed");
        }
        break;
    }

    default:
        break;
    }
}

// --- DBus calls ---
static char *create_remote_desktop_session(GDBusConnection *conn, GError **error) {
    GVariant *result;
    char *session_path = NULL;

    result = g_dbus_connection_call_sync(conn,
        "org.gnome.Mutter.RemoteDesktop",
        "/org/gnome/Mutter/RemoteDesktop",
        "org.gnome.Mutter.RemoteDesktop",
        "CreateSession",
        NULL, NULL,
        G_DBUS_CALL_FLAGS_NONE, -1, NULL, error);

    if (!result) return NULL;

    g_variant_get(result, "(o)", &session_path);
    g_variant_unref(result);
    return session_path;
}

static gboolean session_start(GDBusConnection *conn, const char *session_path, GError **error) {
    GVariant *result;

    result = g_dbus_connection_call_sync(conn,
        "org.gnome.Mutter.RemoteDesktop",
        session_path,
        "org.gnome.Mutter.RemoteDesktop.Session",
        "Start",
        NULL, NULL,
        G_DBUS_CALL_FLAGS_NONE, -1, NULL, error);

    if (!result) return FALSE;
    g_variant_unref(result);
    return TRUE;
}

static int session_connect_to_eis(GDBusConnection *conn, const char *session_path, GError **error) {
    GVariant *result;
    int fd = -1;

    LOG("[dbus] Building params for ConnectToEIS...");

    // Build parameters: (a{sv}) — a tuple with one empty dict
    GVariantBuilder builder;
    g_variant_builder_init(&builder, G_VARIANT_TYPE_TUPLE);
    g_variant_builder_open(&builder, G_VARIANT_TYPE("a{sv}"));
    g_variant_builder_close(&builder);
    GVariant *params = g_variant_builder_end(&builder);

    LOG("[dbus] Params built, calling ConnectToEIS...");

    GUnixFDList *fd_list = NULL;
    result = g_dbus_connection_call_with_unix_fd_list_sync(conn,
        "org.gnome.Mutter.RemoteDesktop",
        session_path,
        "org.gnome.Mutter.RemoteDesktop.Session",
        "ConnectToEIS",
        params,
        NULL,
        G_DBUS_CALL_FLAGS_NONE,
        -1,
        NULL,
        &fd_list,
        NULL,
        error);

    if (!result) {
        LOG("[dbus] ConnectToEIS failed: %s", (*error)->message);
        return -1;
    }

    LOG("[dbus] ConnectToEIS succeeded, parsing result...");

    gint32 handle;
    g_variant_get(result, "(h)", &handle);
    LOG("[dbus] Handle: %d, fd_list size: %d", handle,
            fd_list ? g_unix_fd_list_get_length(fd_list) : -1);

    if (fd_list && g_unix_fd_list_get_length(fd_list) > 0) {
        fd = g_unix_fd_list_get(fd_list, handle, error);
        if (fd < 0) {
            LOG("[dbus] Failed to get fd: %s", (*error)->message);
        }
    } else {
        LOG("[dbus] No fd_list or empty");
    }

    g_variant_unref(result);
    if (fd_list) g_object_unref(fd_list);
    return fd;
}

// --- stdin command handling ---
static void process_command(const char *line) {
    char cmd[64];
    double x, y;
    int button, keycode, state;

    if (sscanf(line, "%63s", cmd) != 1) return;

    if (strcmp(cmd, "quit") == 0) {
        exit(0);
    }

    if (!ready) {
        printf("ERROR not ready\n");
        fflush(stdout);
        return;
    }

    if (strcmp(cmd, "move_abs") == 0) {
        if (sscanf(line, "%*s %lf %lf", &x, &y) != 2) {
            printf("ERROR usage: move_abs X Y\n");
            fflush(stdout);
            return;
        }
        if (!ei_pointer || !ei_device_has_capability(ei_pointer, EI_DEVICE_CAP_POINTER_ABSOLUTE)) {
            printf("ERROR no absolute pointer device\n");
            fflush(stdout);
            return;
        }
        ei_device_pointer_motion_absolute(ei_pointer, x, y);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "move_rel") == 0) {
        if (sscanf(line, "%*s %lf %lf", &x, &y) != 2) {
            printf("ERROR usage: move_rel DX DY\n");
            fflush(stdout);
            return;
        }
        if (!ei_pointer || !ei_device_has_capability(ei_pointer, EI_DEVICE_CAP_POINTER)) {
            printf("ERROR no pointer device\n");
            fflush(stdout);
            return;
        }
        ei_device_pointer_motion(ei_pointer, x, y);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "click") == 0) {
        if (sscanf(line, "%*s %d", &button) != 1) {
            printf("ERROR usage: click BUTTON\n");
            fflush(stdout);
            return;
        }
        // Convert X11-style button (1=left, 2=middle, 3=right) to evdev BTN_*
        int evdev_button = button;
        if (button == 1) evdev_button = 0x110;      // BTN_LEFT
        else if (button == 2) evdev_button = 0x112; // BTN_MIDDLE
        else if (button == 3) evdev_button = 0x111; // BTN_RIGHT

        if (!ei_pointer) {
            printf("ERROR no pointer device\n");
            fflush(stdout);
            return;
        }
        ei_device_button_button(ei_pointer, evdev_button, 1);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        usleep(20000);
        ei_device_button_button(ei_pointer, evdev_button, 0);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "press") == 0) {
        if (sscanf(line, "%*s %d", &button) != 1) {
            printf("ERROR usage: press BUTTON\n");
            fflush(stdout);
            return;
        }
        int evdev_button = button;
        if (button == 1) evdev_button = 0x110;
        else if (button == 2) evdev_button = 0x112;
        else if (button == 3) evdev_button = 0x111;

        if (!ei_pointer) {
            printf("ERROR no pointer device\n");
            fflush(stdout);
            return;
        }
        ei_device_button_button(ei_pointer, evdev_button, 1);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "release") == 0) {
        if (sscanf(line, "%*s %d", &button) != 1) {
            printf("ERROR usage: release BUTTON\n");
            fflush(stdout);
            return;
        }
        int evdev_button = button;
        if (button == 1) evdev_button = 0x110;
        else if (button == 2) evdev_button = 0x112;
        else if (button == 3) evdev_button = 0x111;

        if (!ei_pointer) {
            printf("ERROR no pointer device\n");
            fflush(stdout);
            return;
        }
        ei_device_button_button(ei_pointer, evdev_button, 0);
        ei_device_frame(ei_pointer, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "key") == 0) {
        if (sscanf(line, "%*s %d %d", &keycode, &state) != 2) {
            printf("ERROR usage: key KEYCODE STATE\n");
            fflush(stdout);
            return;
        }
        if (!ei_keyboard) {
            printf("ERROR no keyboard device\n");
            fflush(stdout);
            return;
        }
        ei_device_keyboard_key(ei_keyboard, (uint32_t)keycode, (state != 0));
        ei_device_frame(ei_keyboard, 0);
        ei_dispatch(ei_ctx);
        printf("OK\n");
        fflush(stdout);

    } else if (strcmp(cmd, "type") == 0) {
        const char *text = line + 5;
        if (!ei_keyboard) {
            printf("ERROR no keyboard device\n");
            fflush(stdout);
            return;
        }

        for (const char *p = text; *p; p++) {
            int kc = char_to_keycode(*p);
            if (kc < 0) {
                LOG("[warn] Unknown char: '%c' (0x%02x)", *p, *p);
                continue;
            }

            if (needs_shift(*p)) {
                ei_device_keyboard_key(ei_keyboard, 42, 1);
                ei_device_frame(ei_keyboard, 0);
                ei_dispatch(ei_ctx);
                usleep(1000);
            }

            ei_device_keyboard_key(ei_keyboard, kc, 1);
            ei_device_frame(ei_keyboard, 0);
            ei_dispatch(ei_ctx);
            usleep(1000);

            ei_device_keyboard_key(ei_keyboard, kc, 0);
            ei_device_frame(ei_keyboard, 0);
            ei_dispatch(ei_ctx);
            usleep(1000);

            if (needs_shift(*p)) {
                ei_device_keyboard_key(ei_keyboard, 42, 0);
                ei_device_frame(ei_keyboard, 0);
                ei_dispatch(ei_ctx);
                usleep(1000);
            }
        }
        printf("OK\n");
        fflush(stdout);

    } else {
        printf("ERROR unknown command: %s\n", cmd);
        fflush(stdout);
    }
}

// --- Main ---
int main(int argc, char **argv) {
    GDBusConnection *conn = NULL;
    GError *error = NULL;
    char *session_path = NULL;
    int eis_fd = -1;
    struct pollfd fds[2];

    // Parse flags
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "-q") == 0 || strcmp(argv[i], "--quiet") == 0) {
            quiet = 1;
        }
    }

    // --- 1. Connect to session bus ---
    conn = g_bus_get_sync(G_BUS_TYPE_SESSION, NULL, &error);
    if (!conn) {
        LOG("Failed to connect to session bus: %s", error->message);
        return 1;
    }

    // --- 2. Create RemoteDesktop session ---
    session_path = create_remote_desktop_session(conn, &error);
    if (!session_path) {
        LOG("Failed to create RemoteDesktop session: %s", error->message);
        return 1;
    }
    LOG("[dbus] Created session: %s", session_path);

    // --- 3. Start session ---
    if (!session_start(conn, session_path, &error)) {
        LOG("Failed to start session: %s", error->message);
        return 1;
    }
    LOG("[dbus] Session started");
    fflush(stderr);

    // --- 4. ConnectToEIS ---
    LOG("[dbus] About to call session_connect_to_eis...");
    fflush(stderr);
    eis_fd = session_connect_to_eis(conn, session_path, &error);
    if (eis_fd < 0) {
        LOG("Failed to connect to EIS: %s", error->message);
        return 1;
    }
    LOG("[dbus] ConnectToEIS returned fd %d", eis_fd);

    // --- 5. Set up libei ---
    ei_ctx = ei_new_sender(NULL);
    if (!ei_ctx) {
        LOG("Failed to create ei context");
        return 1;
    }

    ei_log_set_priority(ei_ctx, EI_LOG_PRIORITY_ERROR);

    ei_setup_backend_fd(ei_ctx, eis_fd);

    // --- 6. Event loop: wait for connection and device setup ---
    int ei_fd = ei_get_fd(ei_ctx);
    LOG("[ei] ei fd: %d, waiting for connection...", ei_fd);

    while (!ready) {
        struct pollfd pfd = { .fd = ei_fd, .events = POLLIN };
        int ret = poll(&pfd, 1, 5000);

        if (ret < 0) {
            perror("poll");
            return 1;
        }
        if (ret == 0) {
            LOG("[ei] Timeout waiting for EIS connection");
            return 1;
        }

        if (pfd.revents & (POLLIN | POLLHUP)) {
            ei_dispatch(ei_ctx);
            struct ei_event *event;
            while ((event = ei_get_event(ei_ctx)) != NULL) {
                handle_libei_event(event);
                ei_event_unref(event);
            }
        }
    }

    printf("READY %d %d\n", screen_width, screen_height);
    fflush(stdout);

    // --- 7. Main command loop ---
    fds[0].fd = ei_fd;
    fds[0].events = POLLIN;
    fds[1].fd = STDIN_FILENO;
    fds[1].events = POLLIN;

    char linebuf[4096];
    int linepos = 0;

    while (1) {
        int ret = poll(fds, 2, -1);
        if (ret < 0) {
            perror("poll");
            break;
        }

        if (fds[0].revents & (POLLIN | POLLHUP)) {
            ei_dispatch(ei_ctx);
            struct ei_event *event;
            while ((event = ei_get_event(ei_ctx)) != NULL) {
                handle_libei_event(event);
                ei_event_unref(event);
            }
            if (fds[0].revents & POLLHUP) {
                LOG("[ei] EIS connection closed");
                break;
            }
        }

        if (fds[1].revents & (POLLIN | POLLHUP)) {
            char ch;
            ssize_t n = read(STDIN_FILENO, &ch, 1);
            if (n <= 0) {
                LOG("[stdin] EOF, exiting");
                break;
            }
            if (ch == '\n') {
                linebuf[linepos] = '\0';
                if (linepos > 0) {
                    process_command(linebuf);
                }
                linepos = 0;
            } else if (linepos < (int)sizeof(linebuf) - 1) {
                linebuf[linepos++] = ch;
            }
        }
    }

    g_free(session_path);
    if (ei_ctx) ei_unref(ei_ctx);

    return 0;
}
