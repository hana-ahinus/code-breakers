/* Standalone Windows C pipes animation.
 * Build with MinGW: gcc -O2 -Wall -Wextra pipes_cli.c -o pipes_cli.exe
 */

#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#endif

#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#ifdef _WIN32
#include <windows.h>
#include <conio.h>
#ifndef ENABLE_VIRTUAL_TERMINAL_PROCESSING
#define ENABLE_VIRTUAL_TERMINAL_PROCESSING 0x0004
#endif
#else
#include <sys/ioctl.h>
#include <sys/select.h>
#include <termios.h>
#include <unistd.h>
#endif

#define VERSION "1.0.0"
#define MIN_FPS 20
#define MAX_FPS 100

typedef struct {
    int pipes, fps, steady, limit, style;
    bool random_start, bold, color, keep_style;
} Config;

typedef struct {
    int x, y, direction, style, travelled;
} Pipe;

static int clamp(int value, int low, int high) {
    return value < low ? low : (value > high ? high : value);
}

static void usage(const char *program) {
    printf("Usage: %s [options]\n\n"
           "  -p NUMBER  number of pipes\n"
           "  -f NUMBER  frames per second (20-100)\n"
           "  -s NUMBER  steadiness (5-15)\n"
           "  -r NUMBER  character limit before reset\n"
           "  -R         random start\n"
           "  -B         no bold\n"
           "  -C         no color\n"
           "  -P 0-9     pipe style\n"
           "  -K         keep style on wrap\n"
           "  -v         show version\n"
           "  -h         show help\n\n"
           "Press any key while running to quit.\n", program);
}

static int number(const char *text) {
    char *end;
    long value = strtol(text, &end, 10);
    if (!text[0] || *end) {
        fprintf(stderr, "Invalid number: %s\n", text);
        exit(EXIT_FAILURE);
    }
    return (int)value;
}

static Config parse_args(int argc, char **argv) {
    Config config = {3, 40, 10, 0, 0, false, true, true, false};
    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        if (!strcmp(arg, "-h") || !strcmp(arg, "--help")) {
            usage(argv[0]); exit(EXIT_SUCCESS);
        } else if (!strcmp(arg, "-v") || !strcmp(arg, "--version")) {
            printf("pipes-c v%s\n", VERSION); exit(EXIT_SUCCESS);
        } else if (!strcmp(arg, "-R") || !strcmp(arg, "--random")) config.random_start = true;
        else if (!strcmp(arg, "-B") || !strcmp(arg, "--no-bold")) config.bold = false;
        else if (!strcmp(arg, "-C") || !strcmp(arg, "--no-color")) config.color = false;
        else if (!strcmp(arg, "-K") || !strcmp(arg, "--keep-style")) config.keep_style = true;
        else if (!strcmp(arg, "-S") || !strcmp(arg, "--save-config")) {
            /* Configuration files are deliberately omitted in this standalone version. */
        } else if ((!strcmp(arg, "-p") || !strcmp(arg, "--pipes")) && i + 1 < argc)
            config.pipes = clamp(number(argv[++i]), 1, 100);
        else if ((!strcmp(arg, "-f") || !strcmp(arg, "--fps")) && i + 1 < argc)
            config.fps = clamp(number(argv[++i]), MIN_FPS, MAX_FPS);
        else if ((!strcmp(arg, "-s") || !strcmp(arg, "--steady")) && i + 1 < argc)
            config.steady = clamp(number(argv[++i]), 5, 15);
        else if ((!strcmp(arg, "-r") || !strcmp(arg, "--limit")) && i + 1 < argc)
            config.limit = clamp(number(argv[++i]), 0, 1000000);
        else if ((!strcmp(arg, "-P") || !strcmp(arg, "--pipe-style")) && i + 1 < argc)
            config.style = clamp(number(argv[++i]), 0, 9);
        else {
            fprintf(stderr, "Unknown or incomplete option: %s\n", arg);
            exit(EXIT_FAILURE);
        }
    }
    return config;
}

static void terminal_size(int *width, int *height) {
#ifdef _WIN32
    CONSOLE_SCREEN_BUFFER_INFO info;
    GetConsoleScreenBufferInfo(GetStdHandle(STD_OUTPUT_HANDLE), &info);
    *width = info.srWindow.Right - info.srWindow.Left + 1;
    *height = info.srWindow.Bottom - info.srWindow.Top + 1;
#else
    struct winsize size;
    if (ioctl(STDOUT_FILENO, TIOCGWINSZ, &size) == 0 && size.ws_col > 0) {
        *width = size.ws_col;
        *height = size.ws_row;
    } else {
        *width = 80;
        *height = 24;
    }
#endif
}

static void enable_ansi(void) {
#ifdef _WIN32
    HANDLE output = GetStdHandle(STD_OUTPUT_HANDLE);
    DWORD mode;
    if (GetConsoleMode(output, &mode))
        SetConsoleMode(output, mode | ENABLE_VIRTUAL_TERMINAL_PROCESSING);
#endif
}

#ifdef _WIN32
static bool key_pressed(void) { return _kbhit() != 0; }
static void discard_key(void) { if (_kbhit()) _getch(); }
static void wait_frame(int milliseconds) { Sleep((DWORD)milliseconds); }
static void enable_raw_input(void) { }
#else
static struct termios original_terminal;
static bool raw_input_enabled = false;

static void restore_input(void) {
    if (raw_input_enabled) tcsetattr(STDIN_FILENO, TCSAFLUSH, &original_terminal);
}

static void enable_raw_input(void) {
    struct termios raw;
    if (!isatty(STDIN_FILENO) || tcgetattr(STDIN_FILENO, &original_terminal) != 0) return;
    raw = original_terminal;
    raw.c_lflag &= (tcflag_t)~(ICANON | ECHO);
    raw.c_cc[VMIN] = 0;
    raw.c_cc[VTIME] = 0;
    if (tcsetattr(STDIN_FILENO, TCSAFLUSH, &raw) == 0) {
        raw_input_enabled = true;
        atexit(restore_input);
    }
}

static bool key_pressed(void) {
    struct timeval timeout = {0, 0};
    fd_set input;
    FD_ZERO(&input);
    FD_SET(STDIN_FILENO, &input);
    return select(STDIN_FILENO + 1, &input, NULL, NULL, &timeout) > 0;
}
static void discard_key(void) { char ignored; read(STDIN_FILENO, &ignored, 1); }
static void wait_frame(int milliseconds) {
    struct timespec delay = {milliseconds / 1000, (long)(milliseconds % 1000) * 1000000L};
    nanosleep(&delay, NULL);
}
#endif

static int turn(int direction) {
    const int choices[3] = {-1, 0, 1};
    return (direction + choices[rand() % 3] + 4) % 4;
}

static char pipe_character(int old_direction, int new_direction, int style) {
    static const char corners[] = {'+', '+', '*', '#', '@', 'o', 'x', '%', '&', '#'};
    if (old_direction != new_direction) return corners[style];
    return old_direction % 2 ? '-' : '|';
}

static void reset_pipe(Pipe *pipe, int width, int height, int style, bool random_start) {
    pipe->x = random_start ? rand() % width : width / 2;
    pipe->y = random_start ? rand() % height : height / 2;
    pipe->direction = rand() % 4;
    pipe->style = style;
    pipe->travelled = 0;
}

static void run_pipes(const Config *config) {
    int width, height;
    terminal_size(&width, &height);
    height = height > 1 ? height - 1 : 1;

    char *canvas = calloc((size_t)width * (size_t)height, 1);
    Pipe *pipes = calloc((size_t)config->pipes, sizeof(*pipes));
    if (!canvas || !pipes) {
        free(canvas); free(pipes);
        fputs("Out of memory.\n", stderr);
        return;
    }

    srand((unsigned)time(NULL));
    for (int i = 0; i < config->pipes; ++i)
        reset_pipe(&pipes[i], width, height, config->style, config->random_start);

    enable_ansi();
    enable_raw_input();
    memset(canvas, ' ', (size_t)width * (size_t)height);
    printf("\x1b[2J\x1b[?25l");
    fflush(stdout);

    while (!key_pressed()) {
        for (int i = 0; i < config->pipes; ++i) {
            Pipe *pipe = &pipes[i];
            int old_direction = pipe->direction;
            if (rand() % config->steady == 0) pipe->direction = turn(pipe->direction);
            canvas[pipe->y * width + pipe->x] = pipe_character(old_direction, pipe->direction, pipe->style);

            static const int dx[] = {0, 1, 0, -1};
            static const int dy[] = {-1, 0, 1, 0};
            pipe->x += dx[pipe->direction];
            pipe->y += dy[pipe->direction];
            ++pipe->travelled;

            if (pipe->x < 0 || pipe->x >= width || pipe->y < 0 || pipe->y >= height ||
                (config->limit > 0 && pipe->travelled >= config->limit)) {
                /* Start a new clean drawing when a requested line limit is reached. */
                if (config->limit > 0 && pipe->travelled >= config->limit)
                    memset(canvas, ' ', (size_t)width * (size_t)height);
                reset_pipe(pipe, width, height,
                           config->keep_style ? pipe->style : rand() % 10,
                           config->random_start);
            }
        }

        printf("\x1b[H");
        if (config->color) printf("\x1b[%s32m", config->bold ? "1;" : "");
        for (int y = 0; y < height; ++y)
            printf("%.*s\n", width, canvas + y * width);
        printf("\x1b[0mPress any key to quit");
        fflush(stdout);
        wait_frame(1000 / config->fps);
    }

    discard_key();
    printf("\x1b[0m\x1b[?25h\n");
    free(canvas);
    free(pipes);
}

int main(int argc, char **argv) {
    Config config = parse_args(argc, argv);
    run_pipes(&config);
    return EXIT_SUCCESS;
}
