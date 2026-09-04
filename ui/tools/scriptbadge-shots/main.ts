import { mount } from "svelte";
import Harness from "./Harness.svelte";
import "../../src/app.css";

const target = document.getElementById("shots");
if (!target) throw new Error("Missing #shots mount target");

mount(Harness, { target });
