"""The Perl scanner behind `pyguitest migrate`: what is code, and what is not.

Each case is a construct that used to count a name where Perl would not call
one, or one that a tokenizer has to see through to keep the rest of the file
honest. GetMousePos and SendKeys stand for any export: the first is one of
the six that fail the migrate exit status, so a false positive on it is the
loudest wrong answer the scanner can give.
"""

import time
import unittest

from pyguitest.perlscan import scan


def calls(source):
    return scan(source).calls


def lines_of(source, name="SendKeys"):
    return calls(source).get(name, [])


class TestWhatIsNotCode(unittest.TestCase):
    def assertNoCall(self, source, name="GetMousePos"):
        self.assertEqual(lines_of(source, name), [], source)

    def test_comments_are_skipped(self):
        self.assertNoCall("# GetMousePos() is gone\nprint 1; # GetMousePos\n")

    def test_pod_is_skipped_up_to_cut(self):
        source = "=head1 NAME\n\nGetMousePos\n\n=cut\n\nGetMousePos();\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [7])

    def test_pod_runs_to_the_end_without_a_cut(self):
        self.assertNoCall("print 1;\n\n=pod\n\nGetMousePos();\n")

    def test_pod_only_begins_at_a_line_start(self):
        # `$x =head` is an assignment, not a POD opener.
        source = "my $x =GetMousePos();\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [1])

    def test_everything_after_end_is_data(self):
        self.assertNoCall("print 1;\n__END__\nGetMousePos();\n")

    def test_everything_after_data_is_data(self):
        self.assertNoCall("print 1;\n__DATA__\nGetMousePos();\n")

    def test_single_and_double_quoted_strings(self):
        self.assertNoCall("print 'GetMousePos'; print \"GetMousePos\";\n")

    def test_backticks(self):
        self.assertNoCall("my $o = `GetMousePos`;\n")

    def test_escaped_quote_does_not_end_a_string(self):
        source = 'my $s = "a \\" GetMousePos"; GetMousePos();\n'
        self.assertEqual(lines_of(source, "GetMousePos"), [1])

    def test_quote_like_operators_with_any_delimiter(self):
        for opener in (
            "q(GetMousePos)",
            "qq{GetMousePos}",
            "qw[GetMousePos]",
            "qr<GetMousePos>",
            "q!GetMousePos!",
            "qq|GetMousePos|",
            "q#GetMousePos#",
            "m/GetMousePos/",
            "qr,GetMousePos,",
        ):
            with self.subTest(opener):
                self.assertNoCall(f"my $x = {opener};\n")

    def test_nested_brackets_inside_a_quote(self):
        self.assertNoCall("my $x = q{ { GetMousePos } };\nprint 1;\n")

    def test_two_part_operators_skip_both_halves(self):
        for source in (
            "s/GetMousePos/GetMousePos/;",
            "tr/a/GetMousePos/;",
            "y/a/GetMousePos/;",
            "s{GetMousePos}{GetMousePos}g;",
            "s{a} (GetMousePos);",
        ):
            with self.subTest(source):
                self.assertNoCall(source + "\n")

    def test_substitution_expression_flag_body_is_not_code_either(self):
        # Documented limitation, pinned: the replacement of s///e is Perl,
        # but telling it from a plain replacement costs a second lexer.
        self.assertNoCall("s/a/GetMousePos()/e;\n")

    def test_heredoc_bodies(self):
        for opener in ("<<EOT", "<<'EOT'", '<<"EOT"', "<<~EOT"):
            with self.subTest(opener):
                source = f"print {opener};\n  GetMousePos();\nEOT\nGetMousePos();\n"
                self.assertEqual(lines_of(source, "GetMousePos"), [4])

    def test_stacked_heredocs(self):
        source = (
            "print <<ONE, <<TWO;\n"
            "GetMousePos();\n"
            "ONE\n"
            "GetMousePos();\n"
            "TWO\n"
            "GetMousePos();\n"
        )
        self.assertEqual(lines_of(source, "GetMousePos"), [6])

    def test_indented_heredoc_terminator(self):
        source = "print <<~EOT;\n    GetMousePos\n    EOT\nGetMousePos();\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [4])

    def test_unterminated_heredoc_is_ordinary_code(self):
        # A wrong guess must cost the span, not the file.
        source = "print <<EOT;\nGetMousePos();\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [2])

    def test_shift_is_not_a_heredoc(self):
        source = "my $x = 1 << 2; GetMousePos();\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [1])

    def test_unterminated_string_costs_the_span_not_the_file(self):
        self.assertEqual(lines_of("print 'oops;\nGetMousePos();\n", "GetMousePos"), [2])


class TestWhatLooksLikeANameButIsNot(unittest.TestCase):
    def assertNoCall(self, source, name="GetMousePos"):
        self.assertEqual(lines_of(source, name), [], source)

    def test_variables_of_the_same_name(self):
        for variable in (
            "$GetMousePos",
            "@GetMousePos",
            "%GetMousePos",
            "$#GetMousePos",
            "${GetMousePos}",
            "$GetMousePos[0]",
            "$::GetMousePos",
            "$main::GetMousePos",
        ):
            with self.subTest(variable):
                self.assertNoCall(f"print {variable};\n")

    def test_method_calls_and_class_names(self):
        self.assertNoCall("$obj->GetMousePos();\n")
        self.assertNoCall("$obj->\n    GetMousePos();\n")
        self.assertNoCall("GetMousePos->new;\n")
        self.assertNoCall("GetMousePos ->new;\n")

    def test_other_modules_function_of_the_same_name(self):
        self.assertNoCall("Other::Module::GetMousePos();\n")
        self.assertNoCall("Other::GetMousePos();\n")
        self.assertNoCall("GetMousePos::run();\n")

    def test_hash_keys(self):
        for key in (
            "GetMousePos => 1",
            "{GetMousePos}",
            "{ GetMousePos }",
            "{-GetMousePos}",
            "{ -GetMousePos }",
        ):
            with self.subTest(key):
                self.assertNoCall(
                    f"my %h = ({key});\n" if "=>" in key else f"my $v = $h{key};\n"
                )

    def test_a_braced_call_is_not_a_hash_key(self):
        # `{ &name }` and `{ name() }` are code; only a bare word between the
        # braces of a subscript is a string.
        self.assertEqual(lines_of("$SIG{ &GetMousePos } = 1;\n", "GetMousePos"), [1])
        self.assertEqual(
            lines_of("my @r = map { GetMousePos() } @x;\n", "GetMousePos"), [1]
        )
        self.assertEqual(lines_of("sub f { GetMousePos }\n", "GetMousePos"), [1])

    def test_labels_and_jumps(self):
        self.assertNoCall("GetMousePos: while (1) { last GetMousePos; }\n")
        self.assertNoCall("goto GetMousePos;\n")
        self.assertNoCall("for (1) { next GetMousePos; redo GetMousePos }\n")

    def test_definitions_are_not_uses(self):
        self.assertNoCall("sub GetMousePos { 1 }\n")
        self.assertNoCall("package GetMousePos;\n")
        self.assertNoCall("use constant GetMousePos => 1;\n")

    def test_ternary_colon_is_not_a_label(self):
        source = "my $x = $ok ? GetMousePos : 0;\n"
        self.assertEqual(lines_of(source, "GetMousePos"), [1])


class TestWhatIsCounted(unittest.TestCase):
    def test_a_bare_call_with_and_without_parentheses(self):
        self.assertEqual(lines_of("SendKeys('a');\nSendKeys 'b';\n"), [1, 2])

    def test_ampersand_calls_and_references(self):
        self.assertEqual(lines_of("&SendKeys('a');\nmy $r = \\&SendKeys;\n"), [1, 2])

    def test_the_fully_qualified_name(self):
        scanned = scan("X11::GUITest::SendKeys('a');\n")
        self.assertEqual(scanned.calls["SendKeys"], [1])
        self.assertTrue(scanned.imports_guitest)

    def test_a_name_inside_an_interpolated_block_is_not_seen(self):
        # Documented limit: "@{[ SendKeys() ]}" is code in a string, and
        # looking inside strings is what makes the rest unreliable.
        self.assertEqual(lines_of('print "@{[ SendKeys() ]}";\n'), [])

    def test_line_numbers_survive_heredocs_pod_and_multiline_strings(self):
        source = (
            "my $s = 'a\n"
            "b';\n"
            "print <<EOT;\n"
            "x\n"
            "y\n"
            "EOT\n"
            "\n"
            "=pod\n"
            "z\n"
            "=cut\n"
            "SendKeys('a');\n"
        )
        self.assertEqual(lines_of(source), [11])

    def test_a_name_used_twice_on_a_line_is_two_calls(self):
        self.assertEqual(lines_of("SendKeys('a'); SendKeys('b');\n"), [1, 1])

    def test_a_name_that_only_contains_an_export_is_not_one(self):
        self.assertEqual(lines_of("MySendKeys('a'); SendKeysNow('b');\n"), [])

    def test_crlf_line_endings(self):
        self.assertEqual(lines_of("print 1;\r\nSendKeys('a');\r\n"), [2])


class TestDivisionRegexAndSigilAmbiguity(unittest.TestCase):
    def test_division_is_not_a_pattern(self):
        source = "my $x = $a / $b; SendKeys('a'); my $y = $c / $d;\n"
        self.assertEqual(lines_of(source), [1])

    def test_a_pattern_after_an_operator(self):
        source = "if ($x =~ /SendKeys/) { SendKeys('a'); }\n"
        self.assertEqual(lines_of(source), [1])

    def test_a_pattern_after_a_term_word(self):
        self.assertEqual(lines_of("return /SendKeys/ ? 1 : 0;\nSendKeys 'a';\n"), [2])

    def test_empty_pattern_after_split(self):
        self.assertEqual(lines_of("my @c = split //, $s;\nSendKeys('a');\n"), [2])

    def test_defined_or_is_not_an_empty_pattern(self):
        source = "my $x = $y // 0;\nSendKeys('a');\n"
        self.assertEqual(lines_of(source), [2])

    def test_modulus_is_not_a_hash(self):
        source = "my $m = $a % $b; SendKeys('a');\n"
        self.assertEqual(lines_of(source), [1])

    def test_hash_after_a_term_word(self):
        self.assertEqual(lines_of("my %h = (a => 1); keys %h; SendKeys('a');\n"), [1])

    def test_multiplication_is_not_a_typeglob(self):
        self.assertEqual(lines_of("my $p = $a * $b; SendKeys('a');\n"), [1])

    def test_typeglob_assignment_after_a_block(self):
        # A block-closing } is not an operand, so the * after it is a glob.
        source = "sub f { 1 }\n*SendKeys = \\&f;\nSendKeys('a');\n"
        scanned = scan(source)
        self.assertEqual(scanned.calls.get("SendKeys"), [3])

    def test_a_subscript_close_is_an_operand(self):
        source = "my $x = $h{a} / 2; my $y = $h{b} / 2; SendKeys('a');\n"
        self.assertEqual(lines_of(source), [1])

    def test_comma_delimiter_touching_the_operator(self):
        # Net::FTP spells a slash as m,/, -- the comma must open a pattern.
        source = "if ($p =~ m,/,) { SendKeys('a'); }\n"
        self.assertEqual(lines_of(source), [1])

    def test_a_list_holding_the_word_m_is_not_a_pattern(self):
        source = "my @l = (m, q, y); SendKeys('a');\n"
        self.assertEqual(lines_of(source), [1])

    def test_special_variables_that_are_quote_characters(self):
        for variable in ('$"', "$'", "$/", "$\\", "$#", "$;", "$,"):
            with self.subTest(variable):
                source = f"local {variable} = 1;\nSendKeys('a');\n"
                self.assertEqual(lines_of(source), [2])

    def test_hash_keyed_by_y_or_s_is_not_a_transliteration(self):
        source = "my %h = (y => 1, s => 2, q => 3); SendKeys('a');\n"
        self.assertEqual(lines_of(source), [1])


class TestShadowingAndImports(unittest.TestCase):
    def test_a_sub_of_the_same_name_shadows_the_export(self):
        scanned = scan("sub SendKeys { 1 }\nSendKeys('a');\nGetMousePos();\n")
        self.assertNotIn("SendKeys", scanned.calls)
        self.assertEqual(scanned.shadowed, ["SendKeys"])
        self.assertEqual(scanned.calls["GetMousePos"], [3])

    def test_the_definition_may_follow_its_use(self):
        scanned = scan("SendKeys('a');\nsub SendKeys { 1 }\n")
        self.assertNotIn("SendKeys", scanned.calls)
        self.assertEqual(scanned.shadowed, ["SendKeys"])

    def test_use_constant_shadows(self):
        scanned = scan("use constant DEF_WAIT => 5;\nprint DEF_WAIT;\n")
        self.assertEqual(scanned.constants, {})
        self.assertEqual(scanned.shadowed, ["DEF_WAIT"])

    def test_use_constant_hash_form_shadows_each_name(self):
        source = (
            "use constant { DEF_WAIT => 5, M_LEFT => 1 };\nprint DEF_WAIT + M_LEFT;\n"
        )
        scanned = scan(source)
        self.assertEqual(scanned.constants, {})
        self.assertEqual(scanned.shadowed, ["DEF_WAIT", "M_LEFT"])

    def test_values_in_a_constant_list_do_not_shadow(self):
        # M_LEFT is the value here, DEF_WAIT the constant being defined.
        source = "use constant { DEF_WAIT => M_LEFT };\nprint DEF_WAIT, M_LEFT;\n"
        scanned = scan(source)
        self.assertEqual(scanned.shadowed, ["DEF_WAIT"])
        self.assertEqual(scanned.constants["M_LEFT"], [1, 2])

    def test_a_qualified_call_is_not_shadowed(self):
        source = "sub SendKeys { 1 }\nX11::GUITest::SendKeys('a');\nSendKeys('b');\n"
        scanned = scan(source)
        self.assertEqual(scanned.calls["SendKeys"], [2])
        self.assertEqual(scanned.shadowed, ["SendKeys"])

    def test_a_script_that_never_names_the_module_says_so(self):
        scanned = scan("SendKeys('a');\n")
        self.assertFalse(scanned.imports_guitest)
        self.assertEqual(scanned.calls["SendKeys"], [1])

    def test_use_and_require_name_the_module(self):
        for statement in (
            "use X11::GUITest qw(SendKeys);",
            "use X11::GUITest;",
            "require X11::GUITest;",
            "use X11::GUITest ':ALL';",
        ):
            with self.subTest(statement):
                self.assertTrue(scan(statement + "\n").imports_guitest)

    def test_a_comment_or_string_naming_the_module_does_not_count(self):
        self.assertFalse(
            scan("# use X11::GUITest;\nprint 'X11::GUITest';\n").imports_guitest
        )

    def test_a_similarly_named_module_does_not_count(self):
        self.assertFalse(scan("use X11::GUITest::Extra;\n").imports_guitest)


class TestConstants(unittest.TestCase):
    def test_constants_are_reported_apart_from_calls(self):
        scanned = scan(
            "WaitWindowLike('a', undef, DEF_WAIT);\nClickMouseButton(M_LEFT);\n"
        )
        self.assertEqual(sorted(scanned.constants), ["DEF_WAIT", "M_LEFT"])
        self.assertEqual(sorted(scanned.calls), ["ClickMouseButton", "WaitWindowLike"])

    def test_every_cursor_shape_folds_onto_one_name(self):
        scanned = scan("IsWindowCursor($w, XC_ARROW);\nIsWindowCursor($w, XC_WATCH);\n")
        self.assertEqual(scanned.constants, {"XC_*": [1, 2]})

    def test_an_unknown_xc_name_is_still_a_cursor_shape(self):
        # XC_ names are matched by shape, not by the ~85-entry list.
        self.assertEqual(scan("print XC_NOT_REAL;\n").constants, {"XC_*": [1]})

    def test_a_constant_in_a_string_or_comment_is_not_one(self):
        self.assertEqual(scan("# M_LEFT\nprint 'DEF_WAIT';\n").constants, {})

    def test_a_constant_as_a_hash_key_is_not_one(self):
        self.assertEqual(
            scan("my %h = (M_LEFT => 1); print $h{DEF_WAIT};\n").constants, {}
        )


class TestRealisticScript(unittest.TestCase):
    SOURCE = """\
#!/usr/bin/perl
# Drives gedit. Uses GetMousePos only in this comment.
use strict;
use X11::GUITest qw(:ALL);

=head1 NOTES

IsKeyPressed was removed.

=cut

my %opts = (StartApp => 'gedit', GetMousePos => 1);
my $note = "SendKeys is run later";
my $text = <<'EOT';
GetScreenDepth();
EOT

StartApp($opts{StartApp});
my ($win) = WaitWindowLike('gedit', undef, DEF_WAIT);
SendKeys('Hello~');
$obj->SendKeys('not ours');
print "done\\n" if $win / 2 > 1;
SendKeys('bye');
"""

    def test_only_the_real_calls_are_found(self):
        scanned = scan(self.SOURCE)
        self.assertEqual(
            scanned.calls,
            {"StartApp": [18], "WaitWindowLike": [19], "SendKeys": [20, 23]},
        )
        self.assertEqual(scanned.constants, {"DEF_WAIT": [19]})
        self.assertEqual(scanned.shadowed, [])
        self.assertTrue(scanned.imports_guitest)


class TestRobustness(unittest.TestCase):
    def test_empty_and_blank_sources(self):
        for source in ("", "\n", "   \n\t\n"):
            with self.subTest(repr(source)):
                scanned = scan(source)
                self.assertEqual((scanned.calls, scanned.constants), ({}, {}))

    def test_a_source_ending_mid_construct_does_not_raise(self):
        for source in (
            "s/a/",
            "q{",
            "m",
            "qw(",
            "<<",
            "<<EOT",
            "$",
            "@",
            "%",
            "=pod",
            "sub",
            "use constant {",
            "'",
            '"',
            "`",
            "$#",
            "${",
            "{ -",
            "x =>",
            "print <<~",
            "y/a/b",
        ):
            with self.subTest(source):
                scan(source)

    def test_non_ascii_identifiers_and_text(self):
        source = "my $é = 'ü'; print \"日本語\"; SendKeys('a');\n"
        self.assertEqual(lines_of(source), [1])

    def test_a_file_of_unterminated_constructs_stays_fast(self):
        # Each unterminated quote used to be searched to the end of the file,
        # which is quadratic; past a budget they are no longer looked for.
        source = "q{ s/ <<EOT ' \" `\n" * 20000 + "SendKeys('a');\n"
        start = time.perf_counter()
        scan(source)
        self.assertLess(time.perf_counter() - start, 5.0)

    def test_deeply_nested_braces(self):
        source = "{" * 5000 + "SendKeys('a');" + "}" * 5000 + "\n"
        self.assertEqual(lines_of(source), [1])

    def test_unbalanced_closing_braces(self):
        self.assertEqual(lines_of("}}}} SendKeys('a');\n"), [1])


if __name__ == "__main__":
    unittest.main()
