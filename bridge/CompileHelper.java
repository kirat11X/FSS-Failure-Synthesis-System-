package bridge;

import javax.tools.JavaCompiler;
import javax.tools.ToolProvider;

public class CompileHelper {
    public static void main(String[] args) {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        if (compiler == null) {
            System.err.println("No JavaCompiler available from ToolProvider");
            System.exit(1);
        }
        int rc = compiler.run(null, null, null, args);
        System.exit(rc);
    }
}
